"""Workspace-bound Project-Ops proposal/task reconciliation.

Project-Ops owns proposal review and Task status. Meeting-Ops owns the local
action-item checkbox. This module only updates the explicit federation linkage
columns on ``ActionItem``; it never mutates ``ActionItem.status`` and never
sends a Project-Ops task-status update.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
from typing import Any, Optional
from urllib.parse import unquote_to_bytes, urlparse

from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)

LOCAL_ONLY = "local_only"
PROPOSED = "proposed"
APPROVED_LINKED = "approved_linked"
REJECTED = "rejected"
SYNC_FAILED = "sync_failed"
LINK_STATES = {
    LOCAL_ONLY,
    PROPOSED,
    APPROVED_LINKED,
    REJECTED,
    SYNC_FAILED,
}

MAX_RECONCILE_ITEMS = 100
SOURCE_TYPE = "MEETING_OPS"
SOURCE_LIFECYCLE_VERSION = "meeting-ops.action-lifecycle.v1"
TASK_STATUSES = {
    "PENDING",
    "IN_PROGRESS",
    "WAITING_FOR_CLIENT",
    "COMPLETED",
    "CANCELLED",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error_code(value: Any, fallback: str) -> str:
    text = str(value or fallback).strip().upper()
    text = "".join(ch for ch in text if ch.isalnum() or ch in {"_", "-"})
    return (text or fallback)[:200]


def _project_ops_public_origin() -> Optional[tuple[str, str]]:
    """Return the one public Project-Ops origin allowed in task backlinks.

    The internal API base URL is deliberately not used here: a callback link
    is presented to a human and must be an explicit public origin. HTTP is
    accepted only for loopback local development.
    """
    configured = os.getenv(
        "PROJECTOPS_PUBLIC_URL", "https://projectops.magicunicorn.dev"
    ).strip()
    parsed = urlparse(configured)
    hostname = (parsed.hostname or "").lower()
    if not parsed.netloc or parsed.path not in {"", "/"}:
        return None
    if parsed.scheme == "https":
        return parsed.scheme, parsed.netloc.lower()
    if parsed.scheme == "http" and hostname in {"localhost", "127.0.0.1", "::1"}:
        return parsed.scheme, parsed.netloc.lower()
    return None


def _valid_task_url(value: Any, expected_task_id: Any) -> Optional[str]:
    """Validate the human backlink and bind its route segment to ``taskId``.

    Comparing only origin/path shape is insufficient: an encoded slash or a
    callback that pairs task A's id with task B's URL can otherwise persist a
    misleading link. Decode the one route segment, reject path separators, and
    require exact identity with the separately supplied lifecycle ``taskId``.
    """
    if (
        not isinstance(value, str)
        or not value.strip()
        or not isinstance(expected_task_id, str)
        or not expected_task_id.strip()
    ):
        return None
    expected = expected_task_id.strip()
    if any(ch in expected for ch in {"/", "\\", "?", "#", "%"}):
        return None
    url = value.strip()
    parsed = urlparse(url)
    expected_origin = _project_ops_public_origin()
    if (
        expected_origin is None
        or (parsed.scheme, parsed.netloc.lower()) != expected_origin
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/dashboard/tasks/")
    ):
        return None
    encoded_task_id = parsed.path.removeprefix("/dashboard/tasks/")
    if not encoded_task_id or "/" in encoded_task_id or "\\" in encoded_task_id:
        return None
    try:
        decoded_task_id = unquote_to_bytes(encoded_task_id).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "/" in decoded_task_id or "\\" in decoded_task_id:
        return None
    return url if decoded_task_id == expected else None


def _parse_remote_updated_at(value: Any) -> Optional[datetime]:
    """Parse the contract timestamp without accepting a local/ambiguous time."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _record_sync_conflict(item: Any, error_code: str) -> None:
    """Expose a stale/conflicting response without erasing the known link."""
    item.project_ops_last_sync_attempt_at = _now()
    item.project_ops_sync_error = _safe_error_code(error_code, "SYNC_CONFLICT")
    item.project_ops_retry_count = int(item.project_ops_retry_count or 0) + 1


def _remote_state_is_regression(item: Any, incoming_state: str) -> bool:
    """Project-Ops terminal lifecycle states never reopen from reconciliation."""
    current = getattr(item, "project_ops_link_state", LOCAL_ONLY) or LOCAL_ONLY
    if current == APPROVED_LINKED and incoming_state != APPROVED_LINKED:
        return True
    if current == REJECTED and incoming_state != REJECTED:
        return True
    # A transient sync failure retains link fields. When a task is already
    # known, only an approved link may restore the lifecycle state.
    if current == SYNC_FAILED and getattr(item, "project_ops_task_id", None):
        return incoming_state != APPROVED_LINKED
    return False


def mark_sync_failed(item: Any, error_code: str) -> None:
    """Record a recoverable federation failure without storing remote bodies."""
    item.project_ops_link_state = SYNC_FAILED
    item.project_ops_last_sync_attempt_at = _now()
    item.project_ops_sync_error = _safe_error_code(
        error_code,
        "PROJECT_OPS_SYNC_FAILED",
    )
    item.project_ops_retry_count = int(item.project_ops_retry_count or 0) + 1


def mark_proposed(item: Any, proposal_id: Optional[str] = None) -> None:
    now = _now()
    item.project_ops_link_state = PROPOSED
    if proposal_id:
        item.project_ops_proposal_id = proposal_id
    # A rolling migration or manually repaired legacy row can contain an
    # unexpected value here. Never let that make a retry endpoint crash.
    if not isinstance(getattr(item, "project_ops_submitted_at", None), datetime):
        item.project_ops_submitted_at = now
    item.project_ops_last_sync_attempt_at = now
    item.project_ops_last_synced_at = now
    item.project_ops_sync_error = None

    # Keep legacy JSON stamps readable during a rolling deploy.
    payload = dict(item.raw_payload or {})
    payload["po_triage_submitted_at"] = (
        item.project_ops_submitted_at.isoformat().replace("+00:00", "Z")
    )
    payload["po_triage_source_type"] = SOURCE_TYPE
    item.raw_payload = payload


def _apply_lifecycle_record(item: Any, record: dict[str, Any]) -> None:
    state = str(record.get("state") or "")
    if state not in LINK_STATES - {LOCAL_ONLY}:
        mark_sync_failed(item, "INVALID_LIFECYCLE_STATE")
        return

    remote_updated_at = _parse_remote_updated_at(record.get("updatedAt"))
    if remote_updated_at is None:
        mark_sync_failed(item, "INVALID_REMOTE_UPDATED_AT")
        return

    current_remote_updated_at = getattr(item, "project_ops_remote_updated_at", None)
    if isinstance(current_remote_updated_at, datetime):
        if current_remote_updated_at.tzinfo is None:
            current_remote_updated_at = current_remote_updated_at.replace(
                tzinfo=timezone.utc
            )
        if remote_updated_at < current_remote_updated_at.astimezone(timezone.utc):
            _record_sync_conflict(item, "STALE_REMOTE_STATE")
            return

    if _remote_state_is_regression(item, state):
        _record_sync_conflict(item, "REMOTE_STATE_REGRESSION")
        return

    proposal_id = record.get("proposalId")
    task_id = record.get("taskId")
    project_number = record.get("projectNumber")
    raw_task_url = record.get("taskUrl")
    task_url = _valid_task_url(raw_task_url, task_id)
    task_status = record.get("taskStatus")
    if not isinstance(proposal_id, str) or not proposal_id.strip():
        mark_sync_failed(item, "INVALID_PROPOSAL_LINK")
        return
    if task_id is not None and (not isinstance(task_id, str) or not task_id.strip()):
        mark_sync_failed(item, "INVALID_TASK_LINK")
        return
    if project_number is not None and not isinstance(project_number, str):
        mark_sync_failed(item, "INVALID_LIFECYCLE_RECORD")
        return
    if raw_task_url is not None and task_url is None:
        mark_sync_failed(item, "INVALID_TASK_LINK")
        return
    if task_status is not None and task_status not in TASK_STATUSES:
        mark_sync_failed(item, "INVALID_TASK_STATUS")
        return
    if state == APPROVED_LINKED and (
        not task_id
        or task_url is None
        or task_status not in TASK_STATUSES
    ):
        mark_sync_failed(item, "INVALID_TASK_LINK")
        return

    incoming_proposal_id = proposal_id.strip()
    incoming_task_id = task_id.strip() if isinstance(task_id, str) else None
    if (
        item.project_ops_proposal_id
        and incoming_proposal_id
        and item.project_ops_proposal_id != incoming_proposal_id
    ):
        _record_sync_conflict(item, "PROPOSAL_LINK_CONFLICT")
        return
    if (
        item.project_ops_task_id
        and incoming_task_id
        and item.project_ops_task_id != incoming_task_id
    ):
        _record_sync_conflict(item, "TASK_LINK_CONFLICT")
        return

    now = _now()
    item.project_ops_link_state = state
    item.project_ops_proposal_id = incoming_proposal_id
    item.project_ops_task_id = incoming_task_id
    item.project_ops_task_url = task_url
    item.project_ops_project_number = project_number or None
    item.project_ops_task_status = task_status
    item.project_ops_last_sync_attempt_at = now
    item.project_ops_last_synced_at = now
    item.project_ops_remote_updated_at = remote_updated_at
    item.project_ops_sync_error = (
        _safe_error_code(record.get("errorCode"), "REMOTE_SYNC_FAILED")
        if state == SYNC_FAILED
        else None
    )
    if state in {PROPOSED, APPROVED_LINKED, REJECTED}:
        item.project_ops_submitted_at = item.project_ops_submitted_at or now

    # Preserve compatibility for old readers/scripts without treating this JSON
    # as the lifecycle source of truth.
    payload = dict(item.raw_payload or {})
    if item.project_ops_proposal_id:
        payload["po_proposal_id"] = item.project_ops_proposal_id
    if item.project_ops_task_id:
        payload["po_task_id"] = item.project_ops_task_id
    if item.project_ops_project_number:
        payload["po_project_number"] = item.project_ops_project_number
    payload["po_synced_at"] = now.isoformat().replace("+00:00", "Z")
    item.raw_payload = payload


def _organization_workspace(db: Session, organization_id: int) -> Optional[str]:
    from auth.models import Organization

    org = (
        db.query(Organization)
        .filter(Organization.id == organization_id)
        .first()
    )
    workspace_id = getattr(org, "workspace_id", None) if org else None
    return workspace_id.strip() if isinstance(workspace_id, str) else None


def _project_ops_client(db: Session, organization_id: int, client: Any = None):
    if client is not None:
        return client, False

    from services.integrations.org_config import resolve_project_ops
    from services.projectops_client import ProjectOpsClient

    cfg = resolve_project_ops(db, organization_id)
    if cfg.source == "org_override":
        return (
            ProjectOpsClient(base_url=cfg.api_base_url, api_key=cfg.api_key),
            True,
        )
    return ProjectOpsClient(), True


async def reconcile_projectops_action_items(
    *,
    db: Session,
    organization_id: int,
    item_ids: Optional[list[int]] = None,
    limit: int = MAX_RECONCILE_ITEMS,
    client: Any = None,
) -> dict[str, int]:
    """Reconcile a bounded set of one organization's non-local action items."""
    if limit < 1 or limit > MAX_RECONCILE_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_RECONCILE_ITEMS}")

    from database.models import ActionItem

    query = db.query(ActionItem).filter(
        ActionItem.organization_id == organization_id,
        ActionItem.project_ops_link_state != LOCAL_ONLY,
    )
    if item_ids is not None:
        bounded_ids = list(dict.fromkeys(int(value) for value in item_ids))
        if len(bounded_ids) > limit:
            raise ValueError("item_ids exceeds the reconciliation limit")
        query = query.filter(ActionItem.id.in_(bounded_ids))
    items = query.order_by(ActionItem.id.asc()).limit(limit).all()
    if not items:
        return {"requested": 0, "reconciled": 0, "failed": 0}

    workspace_id = _organization_workspace(db, organization_id)
    if not workspace_id:
        for item in items:
            mark_sync_failed(item, "WORKSPACE_NOT_PROVISIONED")
        db.commit()
        return {"requested": len(items), "reconciled": 0, "failed": len(items)}

    from services.projectops_token import projectops_federation_token

    bearer = await projectops_federation_token(workspace_id)
    if not isinstance(bearer, str) or not bearer.strip():
        for item in items:
            mark_sync_failed(item, "FEDERATION_TOKEN_UNAVAILABLE")
        db.commit()
        return {"requested": len(items), "reconciled": 0, "failed": len(items)}
    bearer = bearer.strip()

    active_client, owned = _project_ops_client(db, organization_id, client)
    try:
        attempt_at = _now()
        for item in items:
            item.project_ops_last_sync_attempt_at = attempt_at
        db.commit()

        try:
            response = await active_client.get_source_lifecycle(
                [str(item.id) for item in items],
                bearer_override=bearer,
            )
        except Exception as exc:  # noqa: BLE001
            for item in items:
                mark_sync_failed(item, "PROJECT_OPS_UNAVAILABLE")
            db.commit()
            logger.warning(
                "projectops lifecycle reconcile failed org=%s count=%s error=%s",
                organization_id,
                len(items),
                type(exc).__name__,
            )
            return {
                "requested": len(items),
                "reconciled": 0,
                "failed": len(items),
            }

        if response.get("version") != SOURCE_LIFECYCLE_VERSION:
            for item in items:
                mark_sync_failed(item, "UNSUPPORTED_LIFECYCLE_CONTRACT")
            db.commit()
            logger.warning(
                "projectops lifecycle rejected unsupported contract org=%s count=%s",
                organization_id,
                len(items),
            )
            return {
                "requested": len(items),
                "reconciled": 0,
                "failed": len(items),
            }

        records = {
            str(record.get("sourceActionItemId")): record
            for record in response.get("items", [])
            if isinstance(record, dict) and record.get("sourceActionItemId")
        }
        reconciled = 0
        failed = 0
        for item in items:
            record = records.get(str(item.id))
            if record is None:
                mark_sync_failed(item, "PROPOSAL_NOT_FOUND")
                failed += 1
                continue
            _apply_lifecycle_record(item, record)
            if item.project_ops_link_state == SYNC_FAILED:
                failed += 1
            else:
                reconciled += 1
        db.commit()
        logger.info(
            "projectops lifecycle reconciled org=%s requested=%s ok=%s failed=%s",
            organization_id,
            len(items),
            reconciled,
            failed,
        )
        return {
            "requested": len(items),
            "reconciled": reconciled,
            "failed": failed,
        }
    finally:
        if owned:
            await active_client.aclose()


async def reconcile_projectops_session_action_items(
    *,
    db: Session,
    organization_id: int,
    session_id: int,
    limit: int = MAX_RECONCILE_ITEMS,
    client: Any = None,
) -> dict[str, Any]:
    """Refresh one session's remote lifecycle rows with a hard item bound.

    The session and every action item are constrained to the active
    organization before any federation call. Local-only items are omitted:
    opening the UI is a read/refresh action and must never submit new meeting
    content or create a proposal.
    """
    if limit < 1 or limit > MAX_RECONCILE_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_RECONCILE_ITEMS}")

    from database.models import ActionItem, RecordingSession

    session_exists = (
        db.query(RecordingSession.id)
        .filter(
            RecordingSession.id == session_id,
            RecordingSession.organization_id == organization_id,
        )
        .first()
    )
    if not session_exists:
        raise LookupError("session not found")

    rows = (
        db.query(ActionItem.id)
        .filter(
            ActionItem.session_id == session_id,
            ActionItem.organization_id == organization_id,
            ActionItem.project_ops_link_state != LOCAL_ONLY,
        )
        .order_by(ActionItem.id.asc())
        .limit(limit + 1)
        .all()
    )
    item_ids = [int(row[0]) for row in rows[:limit]]
    truncated = len(rows) > limit
    if not item_ids:
        return {
            "requested": 0,
            "reconciled": 0,
            "failed": 0,
            "item_ids": [],
            "truncated": truncated,
        }

    result = await reconcile_projectops_action_items(
        db=db,
        organization_id=organization_id,
        item_ids=item_ids,
        limit=limit,
        client=client,
    )
    return {
        **result,
        "item_ids": item_ids,
        "truncated": truncated,
    }


async def requeue_projectops_action_item(
    *,
    db: Session,
    organization_id: int,
    item_id: int,
    client: Any = None,
) -> Any:
    """Explicit, one-item operator retry; never broadens to another tenant."""
    from database.models import ActionItem, RecordingSession
    from services.integrations.org_config import resolve_project_ops

    row = (
        db.query(ActionItem, RecordingSession)
        .join(RecordingSession, ActionItem.session_id == RecordingSession.id)
        .filter(
            ActionItem.id == item_id,
            ActionItem.organization_id == organization_id,
            RecordingSession.organization_id == organization_id,
        )
        .first()
    )
    if not row:
        raise LookupError("action item not found")
    item, session = row
    if item.project_ops_link_state not in {
        PROPOSED,
        APPROVED_LINKED,
        SYNC_FAILED,
    }:
        raise ValueError(
            f"state {item.project_ops_link_state} is not requeueable"
        )

    cfg = resolve_project_ops(db, organization_id)
    if not cfg.enabled and cfg.source == "disabled":
        mark_sync_failed(item, "INTEGRATION_DISABLED")
        db.commit()
        return item

    active_client, owned = _project_ops_client(db, organization_id, client)
    try:
        # If a prior delivery reached Project-Ops, retries only reconcile the
        # existing source id. Re-submitting would be needless and can create
        # a review loop during an outage even though the remote uniqueness key
        # prevents a second proposal.
        needs_submission = (
            item.project_ops_link_state == SYNC_FAILED
            and item.project_ops_submitted_at is None
            and not item.project_ops_proposal_id
            and not item.project_ops_task_id
        )
        if needs_submission:
            workspace_id = _organization_workspace(db, organization_id)
            if not workspace_id:
                mark_sync_failed(item, "WORKSPACE_NOT_PROVISIONED")
                db.commit()
                return item

            from services.projectops_token import projectops_federation_token
            from services.projectops_writer import (
                _candidate_source_ref,
                _explicit_project_ref,
            )

            bearer = await projectops_federation_token(workspace_id)
            if not isinstance(bearer, str) or not bearer.strip():
                mark_sync_failed(item, "FEDERATION_TOKEN_UNAVAILABLE")
                db.commit()
                return item
            bearer = bearer.strip()

            item.project_ops_last_sync_attempt_at = _now()
            db.commit()
            try:
                result = await active_client.submit_candidate_action_items(
                    [
                        {
                            "text": item.text,
                            "owner": item.owner or None,
                            "sourceActionItemId": str(item.id),
                            "sourceRef": _candidate_source_ref(
                                session,
                                item,
                                _explicit_project_ref(db, session),
                            ),
                        }
                    ],
                    source_type=SOURCE_TYPE,
                    bearer_override=bearer,
                )
            except Exception as exc:  # noqa: BLE001
                mark_sync_failed(item, "PROJECT_OPS_UNAVAILABLE")
                db.commit()
                logger.warning(
                    "projectops lifecycle requeue failed org=%s item=%s error=%s",
                    organization_id,
                    item_id,
                    type(exc).__name__,
                )
                return item

            proposal_id = None
            for proposal in result.get("proposals", []):
                if not isinstance(proposal, dict):
                    continue
                source_id = proposal.get("sourceActionItemId")
                if source_id is None or str(source_id) == str(item.id):
                    proposal_id = proposal.get("id")
                    break
            mark_proposed(item, proposal_id)
            db.commit()

        await reconcile_projectops_action_items(
            db=db,
            organization_id=organization_id,
            item_ids=[item.id],
            limit=1,
            client=active_client,
        )
        db.refresh(item)
        return item
    finally:
        if owned:
            await active_client.aclose()


__all__ = [
    "APPROVED_LINKED",
    "LINK_STATES",
    "LOCAL_ONLY",
    "MAX_RECONCILE_ITEMS",
    "PROPOSED",
    "REJECTED",
    "SYNC_FAILED",
    "mark_proposed",
    "mark_sync_failed",
    "reconcile_projectops_action_items",
    "reconcile_projectops_session_action_items",
    "requeue_projectops_action_item",
]
