"""Per-session free-form tags CRUD.

Tags are stored as a Postgres TEXT[] on `recording_sessions.tags`. Insertion
order is preserved on disk; the list endpoint emits them as-is so the UI can
decide on display order (alpha sort client-side keeps things stable while
edits land).

Mirrors the auth + org-scoping pattern from sessions_participants.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import RecordingSession


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/simple/recording-sessions", tags=["session-tags"])


MAX_TAG_LEN = 40


class TagIn(BaseModel):
    tag: str = Field(..., min_length=1, max_length=MAX_TAG_LEN)


class TagListOut(BaseModel):
    tags: List[str]
    tag_counts: Dict[str, int] = Field(default_factory=dict)
    total_sessions: int = 0
    untagged_sessions: int = 0


def _resolve_session(
    db: Session,
    organization_id: int,
    session_id: str,
    user: User,
    min_level: str = "view",
) -> RecordingSession:
    from api.session_permissions import resolve_session_for_user

    rec = resolve_session_for_user(db, organization_id, session_id, user, min_level)
    if not rec:
        raise HTTPException(status_code=404, detail="Session not found")
    return rec


def _can_edit(
    session: RecordingSession,
    user: User,
    active_org: ActiveOrganization,
) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    if active_org.role_name in {"owner", "admin", "manager"}:
        return True
    if session.user_id and session.user_id == user.id:
        return True
    return False


def _normalize(raw: object) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [t for t in raw if isinstance(t, str) and t]
    return []


def _clean_tag(tag: str) -> str:
    cleaned = (tag or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Tag must not be empty")
    if len(cleaned) > MAX_TAG_LEN:
        raise HTTPException(
            status_code=400, detail=f"Tag must be <= {MAX_TAG_LEN} characters"
        )
    return cleaned


@router.get("/tags", response_model=TagListOut)
async def list_org_tags(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """All distinct tags used across the org's sessions, sorted alpha
    (case-insensitive) for the filter chip palette."""
    rows = (
        db.query(RecordingSession.tags)
        .filter(RecordingSession.organization_id == active_org.organization.id)
        .all()
    )
    tag_counts: Dict[str, int] = {}
    total_sessions = 0
    untagged_sessions = 0
    for (raw_tags,) in rows:
        total_sessions += 1
        tags = _normalize(raw_tags)
        if not tags:
            untagged_sessions += 1
            continue
        for tag in dict.fromkeys(tags):
            tag_counts[tag] = tag_counts.get(tag, 0) + 1

    tags = sorted(tag_counts.keys(), key=lambda s: s.lower())
    return TagListOut(
        tags=tags,
        tag_counts=tag_counts,
        total_sessions=total_sessions,
        untagged_sessions=untagged_sessions,
    )


@router.get("/{session_id}/tags", response_model=TagListOut)
async def list_session_tags(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user)
    return TagListOut(tags=_normalize(session.tags))


@router.post("/{session_id}/tags", response_model=TagListOut, status_code=201)
async def add_session_tag(
    session_id: str,
    payload: TagIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user, min_level="edit")
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(status_code=403, detail="Not permitted to edit tags")

    new_tag = _clean_tag(payload.tag)
    current = _normalize(session.tags)
    # Case-insensitive dedupe — preserve the existing casing on conflict.
    lower_existing = {t.lower() for t in current}
    if new_tag.lower() not in lower_existing:
        current = current + [new_tag]
        session.tags = current
        db.commit()
        logger.info("tags: add session=%s tag=%s", session.id, new_tag)
    return TagListOut(tags=current)


@router.delete("/{session_id}/tags/{tag}", response_model=TagListOut)
async def delete_session_tag(
    session_id: str,
    tag: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user, min_level="edit")
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(status_code=403, detail="Not permitted to edit tags")

    current = _normalize(session.tags)
    remaining = [t for t in current if t != tag]
    if len(remaining) == len(current):
        raise HTTPException(status_code=404, detail="Tag not found on session")
    session.tags = remaining
    db.commit()
    logger.info("tags: remove session=%s tag=%s", session.id, tag)
    return TagListOut(tags=remaining)
