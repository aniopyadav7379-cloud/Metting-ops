"""Action-items CRUD scoped to the active organization.

Action items live in their own table (see migration 021_action_items)
and are derived from the post-meeting summarizer JSON columns (or
created manually from the UI). This router is the only write path
the frontend uses; the summarizer-driven path is in
`services.action_items_extractor.persist_action_items` and called
inline by the writers that update `recording_sessions.final_summary`.

All endpoints enforce organization scoping via
`get_current_organization`. The optional `session_id` filter on the
list endpoint accepts both the legacy string session_id (UUID-ish)
and the integer primary key, matching the rest of the API surface.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import ActionItem, RecordingSession


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/action-items", tags=["action-items"])


_ALLOWED_STATUSES = ("todo", "doing", "done", "cancelled")
_ALLOWED_SOURCES = ("final_summary", "ai_insights", "manual")


def _iso_or_none(value: object) -> Optional[str]:
    """Serialize only real, valid datetimes from the persistence boundary.

    A legacy or manually-repaired row must not turn an action-items response
    into a 500 merely because one timestamp is malformed.
    """
    if not isinstance(value, datetime):
        return None
    try:
        return value.isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _legacy_iso_or_none(value: object) -> Optional[str]:
    """Return a legacy JSON timestamp only when it is valid ISO-8601."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return value.strip()


class ActionItemOut(BaseModel):
    id: int
    session_id: int
    session_key: str
    session_title: Optional[str] = None
    session_created_at: Optional[str] = None
    organization_id: int
    text: str
    owner: Optional[str] = None
    due_date: Optional[str] = None
    status: str
    sort_order: int
    source: str
    created_at: str
    completed_at: Optional[str] = None
    project_ops_link_state: str
    project_ops_proposal_id: Optional[str] = None
    project_ops_task_id: Optional[str] = None
    project_ops_task_url: Optional[str] = None
    project_ops_project_number: Optional[str] = None
    project_ops_task_status: Optional[str] = None
    project_ops_submitted_at: Optional[str] = None
    project_ops_last_sync_attempt_at: Optional[str] = None
    project_ops_last_synced_at: Optional[str] = None
    project_ops_sync_error: Optional[str] = None
    project_ops_retry_count: int
    project_ops_triage_submitted_at: Optional[str] = None


class ActionItemCreate(BaseModel):
    # Accept either int pk or string session_id; resolved server-side.
    session_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=4000)
    owner: Optional[str] = Field(default=None, max_length=200)
    due_date: Optional[datetime] = None
    sort_order: Optional[int] = None


class ActionItemPatch(BaseModel):
    text: Optional[str] = Field(default=None, min_length=1, max_length=4000)
    owner: Optional[str] = Field(default=None, max_length=200)
    due_date: Optional[datetime] = None
    status: Optional[str] = None


class ProjectOpsSessionReconcileOut(BaseModel):
    requested: int
    reconciled: int
    failed: int
    truncated: bool
    items: List[ActionItemOut]


def _resolve_session(
    db: Session, organization_id: int, session_id: str
) -> RecordingSession:
    """UUID-or-integer-pk lookup scoped to the active org. Same shape as
    api.sessions_participants._resolve_session."""
    rec = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.session_id == session_id,
            RecordingSession.organization_id == organization_id,
        )
        .first()
    )
    if rec:
        return rec
    try:
        pk = int(session_id)
    except (TypeError, ValueError):
        pk = None
    if pk is not None:
        rec = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.id == pk,
                RecordingSession.organization_id == organization_id,
            )
            .first()
        )
    if not rec:
        raise HTTPException(status_code=404, detail="Session not found")
    return rec


def _can_edit_session(
    session: RecordingSession,
    user: User,
    active_org: ActiveOrganization,
) -> bool:
    """Org admin/manager, session creator, or superuser may mutate the
    session's action items. Matches the auth gate for participants."""
    if getattr(user, "is_superuser", False):
        return True
    role = (active_org.role or "").lower() if getattr(active_org, "role", None) else ""
    if role in {"admin", "manager", "owner"}:
        return True
    if session.user_id and session.user_id == user.id:
        return True
    return False


def _to_out(item: ActionItem, session: Optional[RecordingSession]) -> ActionItemOut:
    raw_payload = item.raw_payload if isinstance(item.raw_payload, dict) else {}
    return ActionItemOut(
        id=item.id,
        session_id=item.session_id,
        session_key=(
            (session.session_id or str(session.id))
            if session is not None
            else str(item.session_id)
        ),
        session_title=(
            (session.title or session.name) if session is not None else None
        ),
        session_created_at=(
            _iso_or_none(session.created_at) if session is not None else None
        ),
        organization_id=item.organization_id,
        text=item.text,
        owner=item.owner,
        due_date=_iso_or_none(item.due_date),
        status=item.status,
        sort_order=item.sort_order,
        source=item.source,
        created_at=_iso_or_none(item.created_at) or "",
        completed_at=_iso_or_none(item.completed_at),
        project_ops_link_state=item.project_ops_link_state or "local_only",
        project_ops_proposal_id=item.project_ops_proposal_id,
        project_ops_task_id=item.project_ops_task_id or raw_payload.get("po_task_id"),
        project_ops_task_url=item.project_ops_task_url,
        project_ops_project_number=(
            item.project_ops_project_number
            or raw_payload.get("po_project_number")
        ),
        project_ops_task_status=item.project_ops_task_status,
        project_ops_submitted_at=_iso_or_none(item.project_ops_submitted_at),
        project_ops_last_sync_attempt_at=_iso_or_none(
            item.project_ops_last_sync_attempt_at
        ),
        project_ops_last_synced_at=_iso_or_none(item.project_ops_last_synced_at),
        project_ops_sync_error=item.project_ops_sync_error,
        project_ops_retry_count=int(item.project_ops_retry_count or 0),
        project_ops_triage_submitted_at=(
            _iso_or_none(item.project_ops_submitted_at)
            or _legacy_iso_or_none(raw_payload.get("po_triage_submitted_at"))
        ),
    )


@router.get("", response_model=List[ActionItemOut])
async def list_action_items(
    status: Optional[str] = Query(default=None),
    session_id: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """List action items for the active org.

    Default ordering: most recent session first, then sort_order asc
    within a session — matches how the LLM produced them so they
    render the way the user saw them in the summary."""
    org_id = active_org.organization.id

    query = (
        db.query(ActionItem, RecordingSession)
        .join(RecordingSession, ActionItem.session_id == RecordingSession.id)
        .filter(
            ActionItem.organization_id == org_id,
            RecordingSession.organization_id == org_id,
        )
    )

    if status:
        if status not in _ALLOWED_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {_ALLOWED_STATUSES}",
            )
        query = query.filter(ActionItem.status == status)

    if session_id:
        target = _resolve_session(db, org_id, session_id)
        query = query.filter(ActionItem.session_id == target.id)

    rows = (
        query.order_by(
            desc(RecordingSession.created_at),
            ActionItem.sort_order.asc(),
            ActionItem.id.asc(),
        )
        .limit(limit)
        .all()
    )
    return [_to_out(item, sess) for item, sess in rows]


@router.post(
    "/sessions/{session_id}/project-ops/reconcile",
    response_model=ProjectOpsSessionReconcileOut,
)
async def reconcile_session_project_ops_action_items(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Non-submitting, bounded lifecycle refresh for one visible session."""
    org_id = active_org.organization.id
    session = _resolve_session(db, org_id, session_id)

    from services.projectops_lifecycle import (
        reconcile_projectops_session_action_items,
    )

    try:
        result = await reconcile_projectops_session_action_items(
            db=db,
            organization_id=org_id,
            session_id=session.id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Session not found")

    item_ids = result.pop("item_ids")
    rows = (
        db.query(ActionItem)
        .filter(
            ActionItem.organization_id == org_id,
            ActionItem.session_id == session.id,
            ActionItem.id.in_(item_ids),
        )
        .order_by(ActionItem.id.asc())
        .all()
        if item_ids
        else []
    )
    logger.info(
        "action_items: Project-Ops session reconcile session=%s org=%s "
        "user=%s requested=%s",
        session.id,
        org_id,
        current_user.id,
        result["requested"],
    )
    return {
        **result,
        "items": [_to_out(item, session) for item in rows],
    }


@router.get("/{item_id}", response_model=ActionItemOut)
async def get_action_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    item = (
        db.query(ActionItem)
        .filter(ActionItem.id == item_id, ActionItem.organization_id == org_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")
    sess = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.id == item.session_id,
            RecordingSession.organization_id == org_id,
        )
        .first()
    )
    return _to_out(item, sess)


@router.post("", response_model=ActionItemOut, status_code=201)
async def create_action_item(
    payload: ActionItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    session = _resolve_session(db, org_id, payload.session_id)
    if not _can_edit_session(session, current_user, active_org):
        raise HTTPException(
            status_code=403, detail="Not permitted to add action items to this session"
        )

    sort_order = payload.sort_order
    if sort_order is None:
        # Append after existing items for this session so manual adds
        # land at the bottom of the list visually.
        existing_max = (
            db.query(ActionItem.sort_order)
            .filter(ActionItem.session_id == session.id)
            .order_by(ActionItem.sort_order.desc())
            .first()
        )
        sort_order = (existing_max[0] + 1) if existing_max else 0

    item = ActionItem(
        session_id=session.id,
        organization_id=org_id,
        text=payload.text.strip(),
        owner=(payload.owner.strip() if payload.owner else None) or None,
        due_date=payload.due_date,
        status="todo",
        sort_order=sort_order,
        source="manual",
        raw_payload={"manual": True, "created_by_user_id": current_user.id},
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    logger.info(
        "action_items: manual add session=%s user=%s id=%s",
        session.id,
        current_user.id,
        item.id,
    )
    return _to_out(item, session)


@router.patch("/{item_id}", response_model=ActionItemOut)
async def update_action_item(
    item_id: int,
    payload: ActionItemPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    item = (
        db.query(ActionItem)
        .filter(ActionItem.id == item_id, ActionItem.organization_id == org_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")

    session = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.id == item.session_id,
            RecordingSession.organization_id == org_id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Parent session not found")
    if not _can_edit_session(session, current_user, active_org):
        raise HTTPException(status_code=403, detail="Not permitted to edit this action item")

    fields = payload.model_fields_set
    content_fields = fields.intersection({"text", "owner", "due_date"})
    if content_fields and (
        item.project_ops_link_state in {"proposed", "approved_linked"}
        or (
            item.project_ops_link_state == "sync_failed"
            and item.project_ops_submitted_at is not None
        )
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Project-Ops owns the submitted proposal/task content. "
                "Local completion may still be changed independently."
            ),
        )

    if "status" in fields:
        new_status = (payload.status or "").strip().lower()
        if new_status not in _ALLOWED_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {_ALLOWED_STATUSES}",
            )
        previous = item.status
        item.status = new_status
        if new_status == "done" and previous != "done":
            item.completed_at = datetime.now(timezone.utc)
        elif new_status != "done" and previous == "done":
            # Re-opened. Clear completion stamp so analytics don't
            # double-count the same item if it gets toggled back.
            item.completed_at = None

    if "text" in fields and payload.text is not None:
        item.text = payload.text.strip()

    if "owner" in fields:
        item.owner = (payload.owner.strip() if payload.owner else None) or None

    if "due_date" in fields:
        item.due_date = payload.due_date

    db.commit()
    db.refresh(item)

    logger.info(
        "action_items: patch id=%s user=%s fields=%s",
        item.id,
        current_user.id,
        sorted(fields),
    )
    return _to_out(item, session)


@router.post("/{item_id}/project-ops/requeue", response_model=ActionItemOut)
async def requeue_project_ops_action_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Retry/reconcile exactly one action item's Project-Ops lifecycle."""
    org_id = active_org.organization.id
    item = (
        db.query(ActionItem)
        .filter(ActionItem.id == item_id, ActionItem.organization_id == org_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")
    session = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.id == item.session_id,
            RecordingSession.organization_id == org_id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Parent session not found")
    if not _can_edit_session(session, current_user, active_org):
        raise HTTPException(
            status_code=403,
            detail="Not permitted to requeue this action item",
        )

    try:
        from services.projectops_lifecycle import requeue_projectops_action_item

        await requeue_projectops_action_item(
            db=db,
            organization_id=org_id,
            item_id=item_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Action item not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    db.refresh(item)
    logger.info(
        "action_items: Project-Ops requeue id=%s org=%s user=%s",
        item.id,
        org_id,
        current_user.id,
    )
    return _to_out(item, session)


@router.delete("/{item_id}", status_code=204)
async def delete_action_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    item = (
        db.query(ActionItem)
        .filter(ActionItem.id == item_id, ActionItem.organization_id == org_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")

    session = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.id == item.session_id,
            RecordingSession.organization_id == org_id,
        )
        .first()
    )
    if session and not _can_edit_session(session, current_user, active_org):
        raise HTTPException(status_code=403, detail="Not permitted to delete this action item")
    if (
        item.project_ops_link_state in {"proposed", "approved_linked"}
        or item.project_ops_task_id
        or item.project_ops_proposal_id
        or (
            item.project_ops_link_state == "sync_failed"
            and item.project_ops_submitted_at is not None
        )
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "This item has Project-Ops lifecycle history and cannot be "
                "deleted from Meeting-Ops."
            ),
        )

    db.delete(item)
    db.commit()
    logger.info("action_items: delete id=%s user=%s", item_id, current_user.id)
    return None
