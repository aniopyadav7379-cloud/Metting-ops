"""Write-side action helpers for Meeting-Ops agent actions.

These helpers are deliberately split into:
  - proposal builders: validate inputs and generate preview/diff payloads
  - confirm-time executors: re-fetch live state, verify it still matches,
    then apply the mutation atomically

The generic propose/confirm/cancel machinery lives in
``services.agent_actions``. This module only knows how to describe and
apply the individual tools.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.orm import Session

from auth.models import User, UserOrganization
from auth.organization import ActiveOrganization
from database.models import RecordingSession

MAX_TITLE_LEN = 200
MAX_TAG_LEN = 40


class DriftError(HTTPException):
    """409 raised when live state has drifted from the proposal's before-snapshot.

    The caller should re-propose. We subclass HTTPException so the FastAPI
    layer renders it as a 409 the same way the existing
    ``raise HTTPException(409, "state changed, please re-propose")`` calls do,
    but tests + agents can match on ``DriftError`` specifically when they
    need to distinguish drift from other 409s.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=409, detail=detail)


def _coerce_for_compare(value: Any) -> Any:
    """Normalize values that round-trip through proposal JSON.

    The before-snapshot in a proposal is serialized to JSON, so datetimes
    become ISO strings and UUIDs become strings. When we re-fetch the
    live row at confirm time those fields are still typed objects. Match
    them by canonicalizing to JSON-friendly forms before comparing.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "hex") and not isinstance(value, (bytes, bytearray)):
        # uuid.UUID etc.
        return str(value)
    return value


def _assert_no_drift(before: dict[str, Any], current: dict[str, Any]) -> None:
    """Confirm-time drift check.

    Compares EVERY field captured in the proposal's ``before`` snapshot
    against the live row. Any mismatch raises :class:`DriftError`, which
    the propose/confirm machinery should surface to the agent so it
    can re-propose with the fresh state.
    """
    for key, expected in before.items():
        observed = current.get(key)
        if _coerce_for_compare(observed) != _coerce_for_compare(expected):
            raise DriftError(
                f"Drift on field {key!r}: proposed={expected!r}, "
                f"observed={observed!r}. Re-propose the action."
            )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _session_label(session: RecordingSession) -> str:
    return session.title or session.name or f"Session {session.id}"


def _session_ref(session: RecordingSession) -> str:
    return session.session_id or str(session.id)


def _normalize_tags(raw: object) -> list[str]:
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def _clean_title(title: Any) -> str:
    cleaned = (title or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Title must not be empty")
    if len(cleaned) > MAX_TITLE_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Title must be <= {MAX_TITLE_LEN} characters",
        )
    return cleaned


def _clean_tag(tag: Any) -> str:
    cleaned = (tag or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Tag must not be empty")
    if len(cleaned) > MAX_TAG_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Tag must be <= {MAX_TAG_LEN} characters",
        )
    return cleaned


def _resolve_session(
    db: Session,
    org_id: int,
    session_id: str,
) -> RecordingSession:
    session = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.organization_id == org_id,
            RecordingSession.session_id == str(session_id),
        )
        .first()
    )
    if session:
        return session
    try:
        pk = int(session_id)
    except (TypeError, ValueError):
        pk = None
    if pk is not None:
        session = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.organization_id == org_id,
                RecordingSession.id == pk,
            )
            .first()
        )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _user_org_role(db: Session, user: User, org_id: int) -> Optional[str]:
    membership = (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id == user.id,
            UserOrganization.organization_id == org_id,
        )
        .first()
    )
    return membership.role if membership else None


def _can_manage_session(db: Session, user: User, org_id: int, session: RecordingSession) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    role = (_user_org_role(db, user, org_id) or "").lower()
    if role in {"admin", "manager"}:
        return True
    return bool(session.user_id and session.user_id == user.id)


def _summary_from_session(session: RecordingSession) -> dict[str, Any]:
    summary = session.final_summary if isinstance(session.final_summary, dict) else {}
    bullets = summary.get("bullets") or summary.get("key_points") or []
    actions = summary.get("actions") or summary.get("action_items") or []
    decisions = summary.get("decisions") or summary.get("key_decisions") or []
    return {
        "executive": summary.get("executive") or summary.get("executive_summary") or "",
        "bullets": bullets,
        "actions": actions,
        "decisions": decisions,
    }


def _proposal_base(
    *,
    action: str,
    preview: str,
    diff: dict[str, Any],
    before: Any,
    after: Any,
    payload: dict[str, Any],
    resource_type: str = "recording_session",
    resource_id: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "action": action,
        "preview": preview,
        "diff": diff,
        "before": before,
        "after": after,
        "payload": payload,
        "resource_type": resource_type,
        "resource_id": resource_id,
    }


def build_create_session_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    title = _clean_title(payload.get("title"))
    description = (payload.get("description") or "").strip() or None
    tags = _normalize_tags(payload.get("tags") or [])
    meeting_type = (payload.get("meeting_type") or "meeting").strip() or "meeting"
    mode = (payload.get("mode") or "upload").strip() or "upload"
    session_name = title

    return _proposal_base(
        action="create_session",
        preview=f'Create session "{title}" in this organization.',
        diff={
            "title": {"from": None, "to": title},
            "description": {"from": None, "to": description},
            "tags": {"from": [], "to": tags},
        },
        before=None,
        after={
            "title": title,
            "name": session_name,
            "description": description,
            "tags": tags,
            "meeting_type": meeting_type,
            "mode": mode,
        },
        payload={
            "title": title,
            "description": description,
            "tags": tags,
            "meeting_type": meeting_type,
            "mode": mode,
        },
        resource_type="recording_session",
        resource_id=None,
    )


def apply_create_session(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    after = proposal.get("after") or {}
    title = _clean_title(after.get("title") or payload.get("title"))
    description = (after.get("description") or payload.get("description") or "").strip() or None
    tags = _normalize_tags(after.get("tags") or payload.get("tags") or [])
    meeting_type = (after.get("meeting_type") or payload.get("meeting_type") or "meeting").strip() or "meeting"
    mode = (after.get("mode") or payload.get("mode") or "upload").strip() or "upload"
    now = _now()

    session = RecordingSession(
        session_id=str(uuid.uuid4()),
        title=title,
        name=title,
        description=description,
        meeting_type=meeting_type,
        mode=mode,
        status="pending",
        created_at=now,
        updated_at=now,
        user_id=user.id,
        organization_id=org_id,
        tags=tags,
        title_user_set=True,
        extra_data={"created_via": "agent_action", "request_context": request_context or {}},
    )
    db.add(session)
    db.flush()

    return {
        "id": session.id,
        "session_id": session.session_id,
        "title": session.title,
        "description": session.description,
        "tags": list(session.tags or []),
        "status": session.status,
    }


def build_rename_session_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to rename this session")

    current_title = session.title or session.name or ""
    new_title = _clean_title(payload.get("title"))
    if new_title == current_title:
        raise HTTPException(status_code=409, detail="Title is unchanged")

    before = {
        "title": current_title,
        "name": session.name,
        "title_user_set": bool(session.title_user_set),
    }
    after = {
        "title": new_title,
        "name": new_title,
        "title_user_set": True,
    }
    return _proposal_base(
        action="rename_session",
        preview=f'Rename session #{session.id} "{current_title}" → "{new_title}"',
        diff={
            "title": {"from": current_title, "to": new_title},
            "name": {"from": session.name, "to": new_title},
            "title_user_set": {"from": bool(session.title_user_set), "to": True},
        },
        before=before,
        after=after,
        payload={"session_id": str(payload.get("session_id")), "title": new_title},
        resource_type="recording_session",
        resource_id=str(session.id),
    )


def apply_rename_session(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to rename this session")

    expected = proposal.get("before") or {}
    current_title = session.title or session.name or ""
    current_name = session.name or ""
    if current_title != expected.get("title") or current_name != expected.get("name"):
        raise HTTPException(
            status_code=409,
            detail="state changed, please re-propose",
        )

    new_title = _clean_title((proposal.get("after") or {}).get("title") or payload.get("title"))
    session.title = new_title[:MAX_TITLE_LEN]
    session.name = new_title[:MAX_TITLE_LEN]
    session.title_user_set = True
    session.updated_at = _now()
    if request_context:
        session.extra_data = {**(session.extra_data or {}), "last_agent_action": request_context}
    db.flush()

    return {
        "id": session.id,
        "session_id": session.session_id or str(session.id),
        "title": session.title,
        "name": session.name,
        "title_user_set": session.title_user_set,
    }


def build_add_tag_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to edit tags")

    tag = _clean_tag(payload.get("tag"))
    current = _normalize_tags(session.tags)
    if tag.lower() in {t.lower() for t in current}:
        raise HTTPException(status_code=409, detail="Tag already exists on this session")

    after = current + [tag]
    return _proposal_base(
        action="add_tag",
        preview=f'Add tag "{tag}" to session #{session.id}',
        diff={"tags": {"from": current, "to": after}},
        before={"tags": current},
        after={"tags": after},
        payload={"session_id": str(payload.get("session_id")), "tag": tag},
        resource_type="recording_session",
        resource_id=str(session.id),
    )


def apply_add_tag(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to edit tags")

    expected = proposal.get("before") or {}
    current = _normalize_tags(session.tags)
    if current != _normalize_tags(expected.get("tags")):
        raise HTTPException(status_code=409, detail="state changed, please re-propose")

    tag = _clean_tag((proposal.get("payload") or {}).get("tag") or payload.get("tag"))
    current_lower = {t.lower() for t in current}
    if tag.lower() in current_lower:
        raise HTTPException(status_code=409, detail="Tag already exists on this session")

    updated = current + [tag]
    session.tags = updated
    session.updated_at = _now()
    if request_context:
        session.extra_data = {**(session.extra_data or {}), "last_agent_action": request_context}
    db.flush()
    return {
        "id": session.id,
        "session_id": session.session_id or str(session.id),
        "tags": updated,
    }


def build_remove_tag_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to edit tags")

    tag = _clean_tag(payload.get("tag"))
    current = _normalize_tags(session.tags)
    if tag.lower() not in {t.lower() for t in current}:
        raise HTTPException(status_code=404, detail="Tag not found on this session")

    updated = [t for t in current if t.lower() != tag.lower()]
    return _proposal_base(
        action="remove_tag",
        preview=f'Remove tag "{tag}" from session #{session.id}',
        diff={"tags": {"from": current, "to": updated}},
        before={"tags": current},
        after={"tags": updated},
        payload={"session_id": str(payload.get("session_id")), "tag": tag},
        resource_type="recording_session",
        resource_id=str(session.id),
    )


def apply_remove_tag(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to edit tags")

    expected = proposal.get("before") or {}
    current = _normalize_tags(session.tags)
    if current != _normalize_tags(expected.get("tags")):
        raise HTTPException(status_code=409, detail="state changed, please re-propose")

    tag = _clean_tag((proposal.get("payload") or {}).get("tag") or payload.get("tag"))
    updated = [t for t in current if t.lower() != tag.lower()]
    if len(updated) == len(current):
        raise HTTPException(status_code=404, detail="Tag not found on this session")

    session.tags = updated
    session.updated_at = _now()
    if request_context:
        session.extra_data = {**(session.extra_data or {}), "last_agent_action": request_context}
    db.flush()
    return {
        "id": session.id,
        "session_id": session.session_id or str(session.id),
        "tags": updated,
    }


def build_trigger_reprocess_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to reprocess this session")
    if not session.audio_file or not Path(session.audio_file).exists():
        raise HTTPException(status_code=410, detail="Session audio file is not on disk; cannot reprocess.")

    metadata = session.processing_metadata if isinstance(session.processing_metadata, dict) else {}
    before_status = metadata.get("reprocess_status")
    after_status = "queued"
    return _proposal_base(
        action="trigger_reprocess",
        preview=f"Trigger a reprocess for session #{session.id} ({_session_label(session)}).",
        diff={
            "status": {"from": session.status, "to": "processing"},
            "reprocess_status": {"from": before_status, "to": after_status},
        },
        before={
            "status": session.status,
            "reprocess_status": before_status,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        },
        after={
            "status": "processing",
            "reprocess_status": after_status,
        },
        payload={"session_id": str(payload.get("session_id"))},
        resource_type="recording_session",
        resource_id=str(session.id),
    )


def apply_trigger_reprocess(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
    background_tasks: Optional[BackgroundTasks] = None,
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to reprocess this session")
    if not session.audio_file or not Path(session.audio_file).exists():
        raise HTTPException(status_code=410, detail="Session audio file is not on disk; cannot reprocess.")

    # Tier gate (WS6 transitive-gate audit): server reprocess is the
    # paid-tier `canonical_reprocess` entitlement. Without this, a free
    # user's agent could reach the GPU reprocess pipeline through the
    # agent-write path, bypassing the gate every direct endpoint in
    # api.recording enforces. Superusers resolve to enterprise.
    from auth.organization import load_organization
    from auth.tier import gate_feature_for_caller

    # billing-1: gate on the SESSION'S workspace plan, not just the agent
    # user's global tier — the agent-write reprocess path must obey per-org
    # billing like the direct endpoints do. Superusers bypass inside the gate.
    gate_feature_for_caller(user, "canonical_reprocess", load_organization(db, org_id))

    expected = proposal.get("before") or {}
    metadata = session.processing_metadata if isinstance(session.processing_metadata, dict) else {}
    current_status = metadata.get("reprocess_status")
    if session.status != expected.get("status") or current_status != expected.get("reprocess_status"):
        raise HTTPException(status_code=409, detail="state changed, please re-propose")

    metadata = dict(metadata)
    metadata["reprocess_status"] = "queued"
    metadata["reprocess_requested_at"] = _now().isoformat()
    metadata["reprocess_requested_by"] = user.id
    session.processing_metadata = metadata
    session.status = "processing"
    session.updated_at = _now()
    if request_context:
        session.extra_data = {**(session.extra_data or {}), "last_agent_action": request_context}

    # Route through the bounded reprocess queue (Arq + per-org daily soft
    # cap) so agent-initiated reprocesses respect the same fairness budget
    # as the always-on path. enqueue_reprocess (async) is scheduled rather
    # than awaited because this function is sync; it falls back to
    # in-process when Arq is disabled/unavailable so a reprocess is never
    # lost.
    from workers.reprocess_workers import enqueue_reprocess

    if background_tasks is not None:
        background_tasks.add_task(enqueue_reprocess, session.id)
        scheduled = "reprocess_queue"
    else:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(enqueue_reprocess(session.id))
            scheduled = "reprocess_queue"
        else:
            raise HTTPException(status_code=500, detail="Unable to schedule reprocess")

    db.flush()
    return {
        "id": session.id,
        "session_id": session.session_id or str(session.id),
        "status": session.status,
        "processing_metadata": session.processing_metadata,
        "scheduled_via": scheduled,
    }


def _email_body_from_session(
    *,
    session: RecordingSession,
    user: User,
    recipient_name: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict[str, str]:
    summary = _summary_from_session(session)
    title = _session_label(session)
    sender_name = user.full_name or user.username or user.email
    recipient = recipient_name or "team"

    subject = f"Follow-up: {title}"

    lines = [
        f"Hi {recipient},",
        "",
        f"Thanks for the discussion on {title}.",
    ]
    if summary["executive"]:
        lines.extend(["", summary["executive"]])
    if summary["decisions"]:
        lines.extend(["", "Decisions:", *[f"- {str(item)}" for item in summary["decisions"][:5]]])
    if summary["actions"]:
        lines.extend(["", "Action items:", *[f"- {str(item)}" for item in summary["actions"][:5]]])
    if notes:
        lines.extend(["", "Additional note:", notes.strip()])
    lines.extend([
        "",
        "Best,",
        sender_name,
    ])
    body = "\n".join(lines).strip()
    return {"subject": subject, "body": body}


def build_draft_followup_email_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to draft email for this session")

    draft = _email_body_from_session(
        session=session,
        user=user,
        recipient_name=(payload.get("recipient_name") or "").strip() or None,
        notes=(payload.get("notes") or "").strip() or None,
    )
    return _proposal_base(
        action="draft_followup_email",
        preview=f'Draft a follow-up email for session #{session.id} ({_session_label(session)}).',
        diff={
            "subject": {"from": None, "to": draft["subject"]},
            "body": {"from": None, "to": draft["body"]},
        },
        before=None,
        after=draft,
        payload={
            "session_id": str(payload.get("session_id")),
            "recipient_name": (payload.get("recipient_name") or "").strip() or None,
            "notes": (payload.get("notes") or "").strip() or None,
        },
        resource_type="recording_session",
        resource_id=str(session.id),
    )


def apply_draft_followup_email(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to draft email for this session")

    draft = proposal.get("after") or {}
    if not draft:
        draft = _email_body_from_session(
            session=session,
            user=user,
            recipient_name=(payload.get("recipient_name") or "").strip() or None,
            notes=(payload.get("notes") or "").strip() or None,
        )
    return {
        "session_id": session.session_id or str(session.id),
        "title": _session_label(session),
        "subject": draft.get("subject", ""),
        "body": draft.get("body", ""),
        "status": "drafted",
    }


# ---------------------------------------------------------------------------
# v1.6 additions: delete_session (high-friction) + start/stop_recording.
# ---------------------------------------------------------------------------


def _required_typed_confirmation_for_delete(session: RecordingSession) -> str:
    """Exact string the user must type to confirm a delete. Predictable + easy
    to type but hard to produce by accident (embeds the session id)."""
    return f"delete-{session.id}"


def build_delete_session_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to delete this session")
    if session.status == "recording":
        raise HTTPException(
            status_code=409,
            detail="Cannot delete while recording; stop the recording first.",
        )

    required = _required_typed_confirmation_for_delete(session)
    title = session.title or session.name or f"session #{session.id}"
    before = {
        "title": session.title,
        "name": session.name,
        "status": session.status,
        "audio_object_key": session.audio_object_key,
    }
    after = {"deleted": True}
    proposal = _proposal_base(
        action="delete_session",
        preview=(
            f'PERMANENT DELETE session #{session.id} "{title}" — this purges the '
            f"transcript, the local audio file, and the Garage object. Irreversible."
        ),
        diff={
            "status": {"from": session.status, "to": "deleted"},
            "audio_object_key": {"from": session.audio_object_key, "to": None},
        },
        before=before,
        after=after,
        payload={"session_id": str(payload.get("session_id"))},
        resource_type="recording_session",
        resource_id=str(session.id),
    )
    # Strong-friction extras: the UI/agent must surface these so the user types
    # the exact string at confirm time; confirm_action re-checks the match.
    proposal["required_typed_confirmation"] = required
    proposal["confirmation_instructions"] = (
        f"To confirm permanent deletion, type exactly: {required}"
    )
    return proposal


def apply_delete_session(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
    typed_confirmation: Optional[str] = None,
) -> dict[str, Any]:
    required = proposal.get("required_typed_confirmation")
    if not required:
        raise HTTPException(status_code=409, detail="Proposal missing typed-confirmation contract.")
    if (typed_confirmation or "").strip() != required:
        raise HTTPException(
            status_code=409,
            detail=f"Typed confirmation does not match. Type exactly: {required}",
        )

    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to delete this session")
    # v3.18.3: full-snapshot drift check — every field captured in `before`
    # must still match. Previously only `status` was compared, which let a
    # mutated `audio_object_key` (e.g. a re-upload between propose and
    # confirm) slip through and get its blob purged.
    expected = proposal.get("before") or {}
    current = {
        "title": session.title,
        "name": session.name,
        "status": session.status,
        "audio_object_key": session.audio_object_key,
    }
    _assert_no_drift(expected, current)
    if session.status == "recording":
        raise HTTPException(status_code=409, detail="Session began recording; stop it first.")

    session_pk = session.id
    session_uuid = session.session_id or str(session.id)
    session_label = session.title or session.name or session_uuid

    # Best-effort cleanup mirroring DELETE /recording-sessions/{id}.
    import os
    if session.audio_file and os.path.exists(session.audio_file):
        try:
            os.remove(session.audio_file)
        except OSError:
            pass
    try:
        from services.session_media import purge_session_media
        purge_session_media(session)
    except Exception:
        pass

    from database.models import Transcription, ChatHistory
    db.query(Transcription).filter(Transcription.session_id == session_pk).delete()
    # Purge per-meeting AI chat history (string session_key, no FK cascade) and,
    # below, the vector-store embeddings — so an agent-initiated delete erases
    # the same data the UI delete now does (see _delete_session_record).
    try:
        db.query(ChatHistory).filter(
            ChatHistory.session_key == session_uuid,
            ChatHistory.organization_id == org_id,
        ).delete(synchronize_session=False)
    except Exception:
        pass
    db.delete(session)
    db.flush()

    # Best-effort vector-store purge (import-on-use; mirrors recording.py).
    try:
        from services.semantic_search_service import semantic_search
        semantic_search.delete_session(session_uuid)
    except Exception:
        pass

    return {
        "id": session_pk,
        "session_id": session_uuid,
        "title": session_label,
        "deleted": True,
    }


def build_start_recording_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to start this recording")
    if session.status == "recording":
        raise HTTPException(status_code=409, detail="Session is already recording.")
    if session.status in ("completed", "processing"):
        raise HTTPException(
            status_code=409,
            detail=f"Session status is {session.status!r} — cannot start a new recording on it.",
        )
    device_id = (payload.get("device_id") or "").strip() or None
    before = {"status": session.status}
    after = {"status": "recording"}
    return _proposal_base(
        action="start_recording",
        preview=(
            f'Start recording on session #{session.id} "{_session_label(session)}"'
            + (f" using device {device_id}" if device_id else " using the default mic")
        ),
        diff={"status": {"from": session.status, "to": "recording"}},
        before=before,
        after=after,
        payload={
            "session_id": str(payload.get("session_id")),
            "device_id": device_id,
        },
        resource_type="recording_session",
        resource_id=str(session.id),
    )


def apply_start_recording(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to start this recording")
    expected = proposal.get("before") or {}
    if session.status != expected.get("status"):
        raise HTTPException(status_code=409, detail="state changed, please re-propose")
    if session.status == "recording":
        raise HTTPException(status_code=409, detail="Session is already recording.")

    import os
    from services.working_audio_service import audio_service
    device_id = payload.get("device_id") or None
    canonical_id = session.session_id or str(session.id)
    success, file_path = audio_service.start_recording(canonical_id, device_id=device_id)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to start recording: {file_path}")
    file_path = os.path.normpath(os.path.abspath(file_path))

    session.status = "recording"
    session.started_at = _now()
    session.audio_file = file_path
    session.updated_at = _now()
    if request_context:
        session.extra_data = {**(session.extra_data or {}), "last_agent_action": request_context}
    db.flush()
    return {
        "id": session.id,
        "session_id": canonical_id,
        "status": session.status,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "audio_file": session.audio_file,
    }


def build_stop_recording_proposal(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to stop this recording")
    if session.status != "recording":
        raise HTTPException(
            status_code=409,
            detail=f"Session is not currently recording (status={session.status!r}).",
        )
    before = {
        "status": session.status,
        "started_at": session.started_at.isoformat() if session.started_at else None,
    }
    after = {"status": "processing"}
    return _proposal_base(
        action="stop_recording",
        preview=f'Stop recording on session #{session.id} "{_session_label(session)}"',
        diff={"status": {"from": "recording", "to": "processing"}},
        before=before,
        after=after,
        payload={"session_id": str(payload.get("session_id"))},
        resource_type="recording_session",
        resource_id=str(session.id),
    )


def apply_stop_recording(
    *,
    db: Session,
    user: User,
    org_id: int,
    payload: dict[str, Any],
    proposal: dict[str, Any],
    request_context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    session = _resolve_session(db, org_id, payload.get("session_id"))
    if not _can_manage_session(db, user, org_id, session):
        raise HTTPException(status_code=403, detail="Not permitted to stop this recording")
    # v3.18.3: full-snapshot drift check on the before-snapshot. Previously
    # only `status` was compared, so a `started_at` shift (re-started session)
    # would pass through and stop the wrong run.
    expected = proposal.get("before") or {}
    current = {
        "status": session.status,
        "started_at": session.started_at.isoformat() if session.started_at else None,
    }
    _assert_no_drift(expected, current)

    import os
    from services.working_audio_service import audio_service
    canonical_id = session.session_id or str(session.id)
    success, audio_file = audio_service.stop_recording(canonical_id)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to stop recording: {audio_file}")
    if audio_file:
        audio_file = os.path.normpath(os.path.abspath(audio_file))
        session.audio_file = audio_file

    session.status = "processing"
    session.ended_at = _now()
    if session.started_at:
        try:
            started = session.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            session.duration = (session.ended_at - started).total_seconds()
        except Exception:
            pass
    session.updated_at = _now()
    if request_context:
        session.extra_data = {**(session.extra_data or {}), "last_agent_action": request_context}
    db.flush()
    return {
        "id": session.id,
        "session_id": canonical_id,
        "status": session.status,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
        "duration": session.duration,
        "audio_file": session.audio_file,
    }
