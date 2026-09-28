"""Proposal/confirm/cancel machinery for Meeting-Ops agent write actions."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import redis.asyncio as redis
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.orm import Session

from auth.models import AuditLog, User
from auth.organization import load_organization
from auth.tier import gate_feature_for_caller
from .agent_write_tools import (
    apply_add_tag,
    apply_create_session,
    apply_delete_session,
    apply_draft_followup_email,
    apply_remove_tag,
    apply_rename_session,
    apply_start_recording,
    apply_stop_recording,
    apply_trigger_reprocess,
    build_add_tag_proposal,
    build_create_session_proposal,
    build_delete_session_proposal,
    build_draft_followup_email_proposal,
    build_remove_tag_proposal,
    build_rename_session_proposal,
    build_start_recording_proposal,
    build_stop_recording_proposal,
    build_trigger_reprocess_proposal,
)

logger = logging.getLogger(__name__)

TOKEN_NAMESPACE = "meeting-ops:agent-actions:"
TOKEN_TTL_SECONDS = 300

_REDIS_CLIENT: Optional[redis.Redis] = None

ACTION_REGISTRY: dict[str, dict[str, Any]] = {
    "create_session": {
        "feature": "agent_write_basic",
        "build": build_create_session_proposal,
        "apply": apply_create_session,
    },
    "rename_session": {
        "feature": "agent_write_basic",
        "build": build_rename_session_proposal,
        "apply": apply_rename_session,
    },
    "add_tag": {
        "feature": "agent_write_basic",
        "build": build_add_tag_proposal,
        "apply": apply_add_tag,
    },
    "remove_tag": {
        "feature": "agent_write_basic",
        "build": build_remove_tag_proposal,
        "apply": apply_remove_tag,
    },
    "trigger_reprocess": {
        "feature": "agent_write_reprocess",
        "build": build_trigger_reprocess_proposal,
        "apply": apply_trigger_reprocess,
    },
    "draft_followup_email": {
        "feature": "agent_write_email_draft",
        "build": build_draft_followup_email_proposal,
        "apply": apply_draft_followup_email,
    },
    # v1.6 — high-friction destructive action + live-state record control
    "delete_session": {
        "feature": "agent_write_basic",
        "build": build_delete_session_proposal,
        "apply": apply_delete_session,
    },
    "start_recording": {
        "feature": "agent_write_basic",
        "build": build_start_recording_proposal,
        "apply": apply_start_recording,
    },
    "stop_recording": {
        "feature": "agent_write_basic",
        "build": build_stop_recording_proposal,
        "apply": apply_stop_recording,
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _proposal_key(proposal_id: str) -> str:
    return f"{TOKEN_NAMESPACE}{proposal_id}"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def _payload_hash(action: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(
        {"action": action, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _redis_client() -> redis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        redis_url = os.getenv("REDIS_URL", "redis://unicorn-redis:6379/6")
        _REDIS_CLIENT = await redis.from_url(redis_url, decode_responses=True)
    return _REDIS_CLIENT


async def _store_proposal(proposal: dict[str, Any]) -> None:
    client = await _redis_client()
    await client.set(
        _proposal_key(proposal["proposal_id"]),
        json.dumps(proposal, default=_json_default),
        ex=TOKEN_TTL_SECONDS,
    )


async def _load_proposal(proposal_id: str) -> Optional[dict[str, Any]]:
    client = await _redis_client()
    raw = await client.get(_proposal_key(proposal_id))
    if not raw:
        return None
    return json.loads(raw)


async def _consume_proposal(proposal_id: str) -> Optional[dict[str, Any]]:
    client = await _redis_client()
    raw = await client.execute_command("GETDEL", _proposal_key(proposal_id))
    if not raw:
        return None
    return json.loads(raw)


async def _delete_proposal(proposal_id: str) -> None:
    client = await _redis_client()
    await client.delete(_proposal_key(proposal_id))


def _audit(
    db: Session,
    *,
    user: User,
    org_id: int,
    action: str,
    proposal_id: str,
    resource_type: str = "agent_action",
    resource_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user.id,
            organization_id=org_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id or proposal_id,
            details=details or {},
        )
    )


def _proposal_response(proposal: dict[str, Any]) -> dict[str, Any]:
    response = {
        "status": "needs_confirmation",
        "action": proposal["action"],
        "preview": proposal["preview"],
        "diff": proposal["diff"],
        "confirmation_token": proposal["proposal_id"],
        "proposal_id": proposal["proposal_id"],
        "expires_at": proposal["expires_at"],
    }
    # Forward optional extra-friction fields (set by high-friction action
    # builders like delete_session) so the UI/agent can render them.
    for k in ("required_typed_confirmation", "confirmation_instructions"):
        if proposal.get(k) is not None:
            response[k] = proposal[k]
    return response


async def propose_action(
    *,
    db: Session,
    user: User,
    org_id: int,
    action: str,
    payload: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    action_key = (action or "").strip()
    entry = ACTION_REGISTRY.get(action_key)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'")

    # billing-1: agent-write capability must be covered by the ACTIVE
    # workspace plan too, not just the user's global tier. Superusers bypass.
    gate_feature_for_caller(user, entry["feature"], load_organization(db, org_id))

    builder = entry["build"]
    proposal_core = builder(db=db, user=user, org_id=org_id, payload=payload)
    proposal_id = f"phc_v1_{secrets.token_urlsafe(18)}"
    expires_at = _utcnow() + timedelta(seconds=TOKEN_TTL_SECONDS)
    serialized_payload = proposal_core["payload"]
    proposal = {
        **proposal_core,
        "proposal_id": proposal_id,
        "user_id": user.id,
        "org_id": org_id,
        "feature": entry["feature"],
        "payload_hash": _payload_hash(action_key, serialized_payload),
        "created_at": _utcnow().isoformat(),
        "expires_at": expires_at.isoformat(),
        "token_consumed": False,
        "request_context": request_context or {},
    }

    await _store_proposal(proposal)
    try:
        _audit(
            db,
            user=user,
            org_id=org_id,
            action="agent_action_proposed",
            proposal_id=proposal_id,
            resource_id=proposal_id,
        details={
            "proposal_id": proposal_id,
            "action": action_key,
            "payload_hash": proposal["payload_hash"],
            "preview": proposal["preview"],
                "diff": proposal["diff"],
                "before": proposal["before"],
            "after": proposal["after"],
            "payload": proposal["payload"],
            "expires_at": proposal["expires_at"],
            "token_consumed": False,
            "request_context": request_context or {},
        },
    )
        db.commit()
    except Exception:
        db.rollback()
        try:
            await _delete_proposal(proposal_id)
        except Exception:
            logger.debug("Failed to clean up proposal token after DB error", exc_info=True)
        raise
    return _proposal_response(proposal)


def _validate_proposal_context(
    *,
    db: Session,
    proposal: dict[str, Any],
    user: User,
    org_id: int,
) -> None:
    if proposal.get("user_id") != user.id:
        raise HTTPException(status_code=403, detail="Proposal token belongs to a different user")
    if proposal.get("org_id") != org_id:
        raise HTTPException(status_code=403, detail="Proposal token belongs to a different organization")
    expires_raw = proposal.get("expires_at")
    try:
        expires_at = datetime.fromisoformat(str(expires_raw))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail="Stored proposal expiry is invalid") from exc
    if _utcnow() > expires_at:
        raise HTTPException(status_code=410, detail="Confirmation token expired")
    entry = ACTION_REGISTRY.get(proposal.get("action") or "")
    if entry is None:
        raise HTTPException(status_code=400, detail="Unknown proposal action")
    # billing-1: re-check the active workspace plan at confirm/cancel time too,
    # mirroring propose_action. Superusers bypass inside the gate.
    gate_feature_for_caller(user, entry["feature"], load_organization(db, org_id))


async def confirm_action(
    *,
    db: Session,
    user: User,
    org_id: int,
    confirmation_token: str,
    typed_confirmation: Optional[str] = None,
    request_context: Optional[dict[str, Any]] = None,
    background_tasks: Optional[BackgroundTasks] = None,
) -> dict[str, Any]:
    proposal = await _consume_proposal(confirmation_token)
    if not proposal:
        raise HTTPException(status_code=404, detail="Confirmation token not found or expired")
    _validate_proposal_context(db=db, proposal=proposal, user=user, org_id=org_id)

    expected_hash = proposal.get("payload_hash")
    actual_hash = _payload_hash(proposal["action"], proposal.get("payload") or {})
    if expected_hash != actual_hash:
        raise HTTPException(status_code=409, detail="payload changed, please re-propose")

    entry = ACTION_REGISTRY[proposal["action"]]
    applier = entry["apply"]
    payload = proposal.get("payload") or {}
    action_kwargs: dict[str, Any] = {}
    if proposal["action"] == "trigger_reprocess":
        action_kwargs["background_tasks"] = background_tasks
    if proposal["action"] == "delete_session":
        # High-friction destructive action: the user must type the exact
        # required string at confirm time; mismatch -> 409 inside applier.
        action_kwargs["typed_confirmation"] = typed_confirmation

    try:
        result = applier(
            db=db,
            user=user,
            org_id=org_id,
            payload=payload,
            proposal=proposal,
            request_context=request_context,
            **action_kwargs,
        )
        proposal["token_consumed"] = True
        _audit(
            db,
            user=user,
            org_id=org_id,
            action="agent_action_confirmed",
            proposal_id=proposal["proposal_id"],
            resource_id=proposal["proposal_id"],
            details={
                "proposal_id": proposal["proposal_id"],
                "action": proposal["action"],
                "payload_hash": proposal["payload_hash"],
                "preview": proposal["preview"],
                "diff": proposal["diff"],
                "before": proposal["before"],
                "after": proposal["after"],
                "payload": proposal["payload"],
                "result": result,
                "token_consumed": True,
                "request_context": request_context or {},
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "status": "applied",
        "action": proposal["action"],
        "proposal_id": proposal["proposal_id"],
        "result": result,
    }


async def cancel_action(
    *,
    db: Session,
    user: User,
    org_id: int,
    confirmation_token: str,
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    proposal = await _consume_proposal(confirmation_token)
    if not proposal:
        raise HTTPException(status_code=404, detail="Confirmation token not found or expired")
    _validate_proposal_context(db=db, proposal=proposal, user=user, org_id=org_id)

    _audit(
        db,
        user=user,
        org_id=org_id,
        action="agent_action_cancelled",
        proposal_id=proposal["proposal_id"],
        resource_id=proposal["proposal_id"],
        details={
            "proposal_id": proposal["proposal_id"],
            "action": proposal["action"],
            "payload_hash": proposal["payload_hash"],
            "preview": proposal["preview"],
            "diff": proposal["diff"],
            "before": proposal["before"],
            "after": proposal["after"],
            "payload": proposal["payload"],
            "token_consumed": True,
            "request_context": request_context or {},
        },
    )
    db.commit()
    return {
        "status": "cancelled",
        "action": proposal["action"],
        "proposal_id": proposal["proposal_id"],
    }
