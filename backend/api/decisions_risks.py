"""Dedicated Decisions (module H) and Risks/Open Questions (module I) views.

The extraction already happens (api.ai_insights._extract_decisions ->
RecordingSession.ai_insights.key_decisions / .follow_ups) — what was
missing was a first-class, cross-meeting, sourced view of that data,
matching the same shape api.action_items.py already gives action items.
No new extraction, no new LLM calls: this reads what's already stored and
attaches session/date/source so each item is traceable, per the Phase 3
"never invent a source" requirement.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import RecordingSession

router = APIRouter(tags=["Decisions & Risks"])


class SourcedInsightItem(BaseModel):
    text: str
    session_id: str
    session_title: str
    session_date: Optional[str] = None


def _list_from_ai_insights(
    db: Session,
    org_id: int,
    field: str,
    session_id: Optional[str],
    limit: int,
) -> List[SourcedInsightItem]:
    query = db.query(RecordingSession).filter(
        RecordingSession.organization_id == org_id,
        RecordingSession.ai_insights.isnot(None),
    )
    if session_id:
        query = query.filter(
            (RecordingSession.session_id == session_id) | (RecordingSession.id == _safe_int(session_id))
        )
    rows = query.order_by(desc(RecordingSession.created_at)).limit(max(limit, 200)).all()

    items: List[SourcedInsightItem] = []
    for session in rows:
        insights = session.ai_insights or {}
        values = insights.get(field) or []
        if not isinstance(values, list):
            continue
        for v in values:
            if not isinstance(v, str) or not v.strip():
                continue
            items.append(
                SourcedInsightItem(
                    text=v,
                    session_id=session.session_id or str(session.id),
                    session_title=session.title or session.name or "",
                    session_date=session.meeting_date.isoformat() if session.meeting_date else None,
                )
            )
            if len(items) >= limit:
                return items
    return items


def _safe_int(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.get("/api/decisions", response_model=List[SourcedInsightItem])
async def list_decisions(
    session_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Every extracted decision across the org's meetings, each attributed
    to the meeting it came from — semantic search over decisions still goes
    through /api/ai-chat/rag/query (Qdrant already indexes summaries), this
    endpoint is the flat, dedicated list view module H calls for."""
    return _list_from_ai_insights(db, active_org.organization.id, "key_decisions", session_id, limit)


@router.get("/api/risks", response_model=List[SourcedInsightItem])
async def list_risks(
    session_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Risks / blockers / open questions (stored as ai_insights.follow_ups)
    across the org's meetings, each attributed to its source meeting."""
    return _list_from_ai_insights(db, active_org.organization.id, "follow_ups", session_id, limit)
