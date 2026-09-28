"""Important Moments API (Phase 3 module J) — timeline of decisions, action
items, explanations, questions, topic transitions, disagreements,
conclusions, and key lecture concepts, each grounded (where possible) to a
real transcript timestamp so the UI can jump to the source."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import ImportantMoment, RecordingSession, Transcription
from services.important_moments import MOMENT_TYPES, extract_moments

router = APIRouter(prefix="/api/moments", tags=["Important Moments"])


class MomentOut(BaseModel):
    id: int
    session_id: str
    session_title: str
    moment_type: str
    description: str
    quote: Optional[str] = None
    timestamp: Optional[float] = None
    speaker: Optional[str] = None


def _resolve_session(db: Session, organization_id: int, session_id: str) -> RecordingSession:
    rec = (
        db.query(RecordingSession)
        .filter(RecordingSession.session_id == session_id, RecordingSession.organization_id == organization_id)
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
            .filter(RecordingSession.id == pk, RecordingSession.organization_id == organization_id)
            .first()
        )
        if rec:
            return rec
    raise HTTPException(status_code=404, detail="Session not found")


def _to_out(moment: ImportantMoment, session_title: str, session_public_id: str) -> MomentOut:
    return MomentOut(
        id=moment.id,
        session_id=session_public_id,
        session_title=session_title,
        moment_type=moment.moment_type,
        description=moment.description,
        quote=moment.quote,
        timestamp=moment.timestamp,
        speaker=moment.speaker,
    )


@router.post("/{session_id}/extract", response_model=List[MomentOut])
async def extract_session_moments(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    session = _resolve_session(db, org_id, session_id)
    transcript = (session.transcript_simple or session.transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="Session has no transcript yet")

    transcriptions = (
        db.query(Transcription)
        .filter(Transcription.session_id == session.id)
        .order_by(Transcription.start_time.asc())
        .all()
    )
    moments = await extract_moments(db, org_id, session.id, transcript, transcriptions)
    public_id = session.session_id or str(session.id)
    title = session.title or session.name or ""
    return [_to_out(m, title, public_id) for m in moments]


@router.get("/{session_id}", response_model=List[MomentOut])
async def get_session_moments(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    session = _resolve_session(db, org_id, session_id)
    rows = (
        db.query(ImportantMoment)
        .filter(ImportantMoment.session_id == session.id, ImportantMoment.organization_id == org_id)
        .order_by(ImportantMoment.timestamp.asc().nullslast())
        .all()
    )
    public_id = session.session_id or str(session.id)
    title = session.title or session.name or ""
    return [_to_out(m, title, public_id) for m in rows]


@router.get("", response_model=List[MomentOut])
async def list_all_moments(
    moment_type: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Org-wide timeline, most recent session first."""
    org_id = active_org.organization.id
    if moment_type is not None and moment_type not in MOMENT_TYPES:
        raise HTTPException(status_code=400, detail=f"moment_type must be one of {MOMENT_TYPES}")

    query = (
        db.query(ImportantMoment, RecordingSession)
        .join(RecordingSession, ImportantMoment.session_id == RecordingSession.id)
        .filter(ImportantMoment.organization_id == org_id, RecordingSession.organization_id == org_id)
    )
    if moment_type:
        query = query.filter(ImportantMoment.moment_type == moment_type)
    rows = query.order_by(desc(RecordingSession.created_at)).limit(limit).all()
    return [
        _to_out(moment, session.title or session.name or "", session.session_id or str(session.id))
        for moment, session in rows
    ]
