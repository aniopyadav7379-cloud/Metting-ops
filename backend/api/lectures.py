"""Lecture domain (Phase 3, module B).

A lecture is NOT a renamed meeting. It shares the exact same capture ->
transcribe -> diarize -> summarize -> Qdrant-index pipeline every
RecordingSession goes through (nothing in that pipeline is lecture-aware,
nor should it be), but it additionally carries structured, lecture-shaped
fields (course, instructor, concepts, definitions, examples, study notes,
questions) that a meeting has no use for — see LectureDetails in
database/models.py and alembic/versions/060_lecture_details.py for why
those live in a satellite table instead of bloating RecordingSession.

A session becomes a lecture by setting RecordingSession.meeting_type =
"lecture" (existing column, no schema change needed for the base
distinction) and creating a matching LectureDetails row via this router.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import LectureDetails, RecordingSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lectures", tags=["Lectures"])

LECTURE_TYPE = "lecture"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class LectureMetadata(BaseModel):
    course: Optional[str] = Field(default=None, max_length=300)
    subject: Optional[str] = Field(default=None, max_length=300)
    instructor: Optional[str] = Field(default=None, max_length=300)
    lecture_number: Optional[int] = Field(default=None, ge=1)


class LectureOut(BaseModel):
    session_id: str
    title: str
    meeting_date: Optional[str] = None
    duration: float
    status: str
    course: Optional[str] = None
    subject: Optional[str] = None
    instructor: Optional[str] = None
    lecture_number: Optional[int] = None
    concepts: List[str] = []
    definitions: List[dict] = []
    examples: List[str] = []
    questions: List[str] = []
    study_notes: Optional[str] = None
    extraction_status: str = "pending"


def _resolve_session(db: Session, organization_id: int, session_id: str) -> RecordingSession:
    """Same lookup shape as api.action_items._resolve_session."""
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


def _to_out(session: RecordingSession, details: Optional[LectureDetails]) -> LectureOut:
    return LectureOut(
        session_id=session.session_id or str(session.id),
        title=session.title or session.name or "",
        meeting_date=session.meeting_date.isoformat() if session.meeting_date else None,
        duration=session.duration or 0.0,
        status=session.status or "",
        course=details.course if details else None,
        subject=details.subject if details else None,
        instructor=details.instructor if details else None,
        lecture_number=details.lecture_number if details else None,
        concepts=(details.concepts or []) if details else [],
        definitions=(details.definitions or []) if details else [],
        examples=(details.examples or []) if details else [],
        questions=(details.questions or []) if details else [],
        study_notes=details.study_notes if details else None,
        extraction_status=details.extraction_status if details else "pending",
    )


# ---------------------------------------------------------------------------
# List / mark-as-lecture / get / update metadata
# ---------------------------------------------------------------------------

@router.get("", response_model=List[LectureOut])
async def list_lectures(
    course: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    query = (
        db.query(RecordingSession, LectureDetails)
        .outerjoin(LectureDetails, LectureDetails.session_id == RecordingSession.id)
        .filter(RecordingSession.organization_id == org_id, RecordingSession.meeting_type == LECTURE_TYPE)
    )
    if course:
        query = query.filter(LectureDetails.course == course)
    rows = query.order_by(desc(RecordingSession.created_at)).limit(limit).all()
    return [_to_out(session, details) for session, details in rows]


@router.put("/{session_id}/mark", response_model=LectureOut)
async def mark_as_lecture(
    session_id: str,
    metadata: LectureMetadata,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Tag an existing (or new) session as a lecture and set its metadata.
    Idempotent — safe to call again to edit course/instructor/etc."""
    org_id = active_org.organization.id
    session = _resolve_session(db, org_id, session_id)
    session.meeting_type = LECTURE_TYPE

    details = db.query(LectureDetails).filter(LectureDetails.session_id == session.id).first()
    if details is None:
        details = LectureDetails(session_id=session.id, organization_id=org_id)
        db.add(details)
    details.course = metadata.course
    details.subject = metadata.subject
    details.instructor = metadata.instructor
    details.lecture_number = metadata.lecture_number
    db.commit()

    # Re-index with source_kind="lecture" so Ask AI's context header and
    # citation UI say "Lecture" rather than the default "Meeting" (Phase
    # 3D/3E). Best-effort: a session with no transcript yet (not uploaded/
    # transcribed) simply has nothing to index yet, which is not an error.
    transcript = (session.transcript_simple or session.transcript or "").strip()
    if transcript:
        try:
            from services.semantic_search_service import semantic_search

            semantic_search.index_session(
                session_id=session.session_id or str(session.id),
                title=session.title or session.name or "",
                transcript=transcript,
                summary=session.summary or None,
                created_at=session.created_at.isoformat() if session.created_at else None,
                organization_id=org_id,
                source_kind="lecture",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to re-index lecture %s with source_kind=lecture: %s", session.id, exc)

    db.refresh(session)
    db.refresh(details)
    return _to_out(session, details)


@router.get("/{session_id}", response_model=LectureOut)
async def get_lecture(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    org_id = active_org.organization.id
    session = _resolve_session(db, org_id, session_id)
    if session.meeting_type != LECTURE_TYPE:
        raise HTTPException(status_code=404, detail="Session is not a lecture")
    details = db.query(LectureDetails).filter(LectureDetails.session_id == session.id).first()
    return _to_out(session, details)


# ---------------------------------------------------------------------------
# Structured extraction: concepts / definitions / examples / questions /
# study notes — genuinely lecture-shaped, not the meeting decisions/risks
# schema reused with different labels.
# ---------------------------------------------------------------------------

_LECTURE_SYSTEM_PROMPT = (
    "You are analyzing a lecture transcript for a student's study notes. "
    "Extract ONLY what the lecturer actually explained — never invent a "
    "concept, definition, or example that isn't in the transcript. Return "
    "ONLY a single valid JSON object, no markdown fences, no prose."
)


def _build_lecture_prompt(transcript: str) -> str:
    return (
        "From this lecture transcript, extract:\n"
        "- concepts: array of key concept names covered (short phrases)\n"
        "- definitions: array of {\"term\": string, \"definition\": string} "
        "for terms the lecturer explicitly defined\n"
        "- examples: array of short descriptions of worked examples or "
        "illustrations the lecturer gave\n"
        "- questions: array of questions the lecturer posed to students, or "
        "questions students asked, verbatim or closely paraphrased\n"
        "- study_notes: a concise study-notes summary (a few paragraphs, "
        "organized by concept) a student could review before an exam\n\n"
        "Return JSON with exactly these five keys.\n\n"
        f"{transcript[:16000]}"
    )


async def _generate_lecture_extraction(db: Session, org_id: Optional[int], transcript: str) -> dict:
    """Mirrors api.ai_insights._extract_decisions's resolve->prompt->parse
    shape, with a lecture schema instead of a meeting one. Degrades to an
    empty-but-valid result (never raises) so a failed extraction never
    blocks the rest of the finalize pipeline."""
    from api.ai_insights import _resolve_llm  # local import: avoid import cycle at module load

    empty = {"concepts": [], "definitions": [], "examples": [], "questions": [], "study_notes": None}
    svc = _resolve_llm(db, org_id, task="quality")
    if svc is None:
        return empty
    try:
        raw = await asyncio.to_thread(
            svc.chat_sync,
            system_prompt=_LECTURE_SYSTEM_PROMPT,
            user_prompt=_build_lecture_prompt(transcript),
            max_tokens=1200,
            temperature=0.3,
        )
        if not raw or not raw.strip():
            return empty
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return empty
        parsed = json.loads(match.group(0))
        return {
            "concepts": [str(c) for c in parsed.get("concepts", []) if isinstance(parsed.get("concepts"), list)][:30],
            "definitions": [d for d in parsed.get("definitions", []) if isinstance(d, dict) and "term" in d and "definition" in d][:30],
            "examples": [str(e) for e in parsed.get("examples", []) if isinstance(parsed.get("examples"), list)][:20],
            "questions": [str(q) for q in parsed.get("questions", []) if isinstance(parsed.get("questions"), list)][:20],
            "study_notes": str(parsed.get("study_notes")) if parsed.get("study_notes") else None,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lecture extraction failed for org=%s: %s", org_id, exc)
        return empty


@router.post("/{session_id}/extract", response_model=LectureOut)
async def extract_lecture_knowledge(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Run (or re-run) concept/definition/example/question/study-notes
    extraction over the session's transcript. Session must already be
    transcribed and marked as a lecture (POST .../mark first)."""
    org_id = active_org.organization.id
    session = _resolve_session(db, org_id, session_id)
    if session.meeting_type != LECTURE_TYPE:
        raise HTTPException(status_code=400, detail="Session must be marked as a lecture first (PUT .../mark)")

    transcript = (session.transcript_simple or session.transcript or "").strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="Session has no transcript yet")

    details = db.query(LectureDetails).filter(LectureDetails.session_id == session.id).first()
    if details is None:
        details = LectureDetails(session_id=session.id, organization_id=org_id)
        db.add(details)
        db.flush()

    details.extraction_status = "running"
    db.commit()

    result = await _generate_lecture_extraction(db, org_id, transcript)

    details.concepts = result["concepts"]
    details.definitions = result["definitions"]
    details.examples = result["examples"]
    details.questions = result["questions"]
    details.study_notes = result["study_notes"]
    details.extraction_status = "completed"
    db.commit()
    db.refresh(session)
    db.refresh(details)
    return _to_out(session, details)
