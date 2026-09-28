"""
Meeting Management API endpoints.

HONESTY AUDIT (2026-06-10): this router was historically a grab-bag and
several endpoints returned FAKE/static data. The frontend calls NONE of the
routes this module uniquely owns (verified by grepping frontend/src — the
only non-/api/simple call, GET /api/recording-sessions/{id} in roomsApi.ts,
is served by api/sessions.py which main.py loads first and therefore wins
routing). The fake endpoints now return an honest HTTP 501 instead of
fabricated data:

  - POST /api/recording-sessions            — was an in-memory mock ("DB
    issues" workaround), unauthenticated, and shadowed by api/sessions.py
    anyway. Real creation lives at POST /api/simple/recording-sessions.
  - POST /api/recording-sessions/{id}/reprocess — pretended to reprocess via
    a background task that just stamped npu_processed=True / 1500ms / 0.95
    confidence without doing any work. Real reprocess lives at
    POST /api/uploads/sessions/{id}/reprocess (Arq queue).
  - GET  /api/system/npu-status             — fabricated "250x real-time"
    NPU metrics (same family as the fake NPU banner removed in the
    pre-onboarding sweep).
  - /api/summarization-templates CRUD       — "persisted" to a process-local
    list that evaporated on restart.
  - GET  /api/analytics/meetings            — written against a pre-multi-org
    model schema: it reads RecordingSession.npu_processed / file_size, which
    DO NOT EXIST on the live model, so it 500'd the moment any session fell
    in range (and returned zeros computed from nothing otherwise). It cannot
    serve real data; 501 until rebuilt (analytics_simple.py is the real
    analytics surface).

Still REAL and intentionally kept:
  - GET /api/recording-sessions/{id}/export — org-scoped, delegates to
    batch_export renderers, covered by tests/test_export.py.
  - PUT /api/recording-sessions/{id}/transcript — real (legacy user_id-scoped)
    transcript replace; uncalled by the frontend but functional.

WAVE-3 CLEANUP (2026-06-16): the two GET duplicates were DELETED — they were
dead code (api/sessions.py, loaded first at the same /api/recording-sessions
paths, shadows them) and also read pre-multi-org columns (file_size /
npu_processed / audio_file_path) absent from the live model. The broken
DELETE/PATCH (DELETE read the nonexistent ``audio_file_path``; PATCH wrote the
nonexistent ``starred`` column; both uncalled by the frontend) now return an
honest 501 pointing at the real CRUD: DELETE/PATCH
/api/simple/recording-sessions/{id}.

The router itself MUST keep loading so /health's routers_loaded count stays
stable.
"""
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.responses import StreamingResponse
from typing import List, Dict, Any
from pydantic import BaseModel
import json

# Import dependencies
from database.database import get_db
from database.models import RecordingSession, Transcription
from sqlalchemy.orm import Session
from auth.dependencies import get_current_user, get_current_organization
from auth.models import User
from auth.organization import ActiveOrganization
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["meetings"])

_NOT_IMPLEMENTED = HTTPException(status_code=501, detail="Not implemented")

class TranscriptUpdate(BaseModel):
    transcript: List[Dict[str, Any]]

# Recording Session Endpoints
@router.post("/recording-sessions")
async def create_session_stub_shadowed_by_simple_recording_db():
    """NOT IMPLEMENTED — was an unauthenticated in-memory mock.

    This route is shadowed by api/sessions.py's POST (loaded first) in the
    live app; real session creation is POST /api/simple/recording-sessions.
    Renamed from create_session (2026-09-18) solely to resolve a duplicate
    FastAPI operation-id warning at startup with api/simple_recording_db.py's
    real create_session — this function is dead/unreachable either way, so
    the rename changes no request path, status code, or response contract.
    """
    raise _NOT_IMPLEMENTED

# GET /api/recording-sessions and GET /api/recording-sessions/{session_id}
# were REMOVED (2026-06-16): dead duplicates. api/sessions.py (loaded first in
# main.py, same /api/recording-sessions paths) shadows them, and they also read
# pre-multi-org columns (file_size / npu_processed / audio_file_path) that don't
# exist on the live model. The real reads live in api/sessions.py.

@router.delete("/recording-sessions/{session_id}")
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
):
    """NOT IMPLEMENTED — the legacy handler read the nonexistent
    ``audio_file_path`` column (500'd before commit) and was never called by
    the frontend. Real deletion: DELETE /api/simple/recording-sessions/{id}."""
    raise _NOT_IMPLEMENTED

@router.patch("/recording-sessions/{session_id}")
async def update_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
):
    """NOT IMPLEMENTED — the legacy handler wrote ``starred`` (not a model
    column) and was never called by the frontend. Real metadata updates:
    PATCH /api/simple/recording-sessions/{id}."""
    raise _NOT_IMPLEMENTED

@router.put("/recording-sessions/{session_id}/transcript")
async def update_transcript(
    session_id: str,
    transcript_data: TranscriptUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Disabled legacy transcript replacement; use the scoped edit API."""
    raise _NOT_IMPLEMENTED

@router.get("/recording-sessions/{session_id}/export")
async def export_session(
    session_id: str,
    format: str = Query(default="txt", pattern="^(txt|md|markdown|json|pdf|docx|srt)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Export session in various formats.

    Org-scoped via ``_get_session_for_export``: the caller's active
    organization must own the session row. Per-user filtering was the legacy
    pre-multi-org behavior and 404'd every valid session once the
    ``organization_id`` column became canonical.

    Supported formats:
      - ``txt``       — plain transcript
      - ``md`` / ``markdown`` — GitHub-flavored markdown summary
      - ``json``      — structured session + transcript payload
      - ``srt``       — subtitle file
      - ``pdf``       — PDF summary (delegated to batch_export)
      - ``docx``      — DOCX summary (delegated to batch_export)
    """
    try:
        # Org-scoped lookup. Accepts either int PK or UUID string session_id.
        from api.batch_export import _get_session_for_export
        session = _get_session_for_export(db, active_org.organization.id, session_id)

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # Get transcripts (legacy Transcription rows — many sessions instead
        # carry diarized JSON on the session row itself; both paths are
        # tolerated below).
        transcripts = db.query(Transcription).filter(
            Transcription.session_id == session.id
        ).order_by(Transcription.start_time).all()

        if format == "txt":
            content = f"Meeting: {session.name}\n"
            content += f"Date: {session.created_at}\n"
            content += f"Duration: {session.duration} seconds\n\n"
            content += "Transcript:\n"
            content += "-" * 50 + "\n"

            for t in transcripts:
                speaker = t.speaker or "Speaker 1"
                content += f"\n[{speaker}]: {t.text}\n"

            return StreamingResponse(
                iter([content.encode()]),
                media_type="text/plain",
                headers={"Content-Disposition": f"attachment; filename=transcript-{session_id}.txt"}
            )

        elif format in ("md", "markdown"):
            # Delegate to batch_export.export_to_markdown so we share the
            # GitHub-flavored renderer used everywhere else. ExportOptions
            # defaults already match the summary-only behavior expected by
            # the SessionDetails markdown button.
            from api.batch_export import export_to_markdown, ExportOptions
            options = ExportOptions(includeTranscript=False)
            content = export_to_markdown(session, options)
            return StreamingResponse(
                iter([content.encode("utf-8")]),
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition": f"attachment; filename=transcript-{session_id}.md"}
            )

        elif format == "json":
            data = {
                "session": {
                    "id": session.id,
                    "name": session.name,
                    "date": session.created_at.isoformat() if session.created_at else None,
                    "duration": session.duration
                },
                "transcript": [
                    {
                        "speaker": t.speaker or "Speaker 1",
                        "text": t.text,
                        "start_time": t.start_time,
                        "end_time": t.end_time,
                        "confidence": t.confidence
                    }
                    for t in transcripts
                ]
            }

            return StreamingResponse(
                iter([json.dumps(data, indent=2).encode()]),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename=transcript-{session_id}.json"}
            )

        elif format == "srt":
            content = ""
            for i, t in enumerate(transcripts, 1):
                start = format_srt_time(t.start_time)
                end = format_srt_time(t.end_time)
                speaker = t.speaker or "Speaker 1"
                content += f"{i}\n"
                content += f"{start} --> {end}\n"
                content += f"[{speaker}] {t.text}\n\n"

            return StreamingResponse(
                iter([content.encode()]),
                media_type="text/plain",
                headers={"Content-Disposition": f"attachment; filename=transcript-{session_id}.srt"}
            )

        elif format == "pdf":
            from api.batch_export import export_to_pdf, ExportOptions
            pdf_bytes = export_to_pdf(session, ExportOptions())
            return StreamingResponse(
                iter([pdf_bytes]),
                media_type="application/pdf",
                headers={"Content-Disposition": f"attachment; filename=transcript-{session_id}.pdf"}
            )

        elif format == "docx":
            from api.batch_export import export_to_docx, ExportOptions
            docx_bytes = export_to_docx(session, ExportOptions())
            return StreamingResponse(
                iter([docx_bytes]),
                media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={"Content-Disposition": f"attachment; filename=transcript-{session_id}.docx"}
            )

        else:  # pragma: no cover — regex above pins the allowed values
            raise HTTPException(status_code=501, detail=f"Export format {format} not yet implemented")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to export session: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/recording-sessions/{session_id}/reprocess")
async def reprocess_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
):
    """NOT IMPLEMENTED — previously FAKED reprocessing (a background task
    stamped npu_processed=True, processing_time=1500, confidence=0.95 without
    doing any work). Real reprocess: POST /api/uploads/sessions/{id}/reprocess.
    """
    raise _NOT_IMPLEMENTED

# NPU Status Endpoint
@router.get("/system/npu-status")
async def get_npu_status():
    """NOT IMPLEMENTED — previously returned fabricated NPU metrics
    ("250x real-time"). There is no NPU in the live inference stack."""
    raise _NOT_IMPLEMENTED

# Analytics Endpoints
@router.get("/analytics/meetings")
async def get_meeting_analytics(
    range: str = Query(default="week", pattern="^(week|month|quarter|year|all)$"),
    current_user: User = Depends(get_current_user),
):
    """NOT IMPLEMENTED — was written against a pre-multi-org schema and read
    RecordingSession.npu_processed / file_size, which do not exist on the
    live model: it 500'd whenever any session fell in the range. The real
    analytics surface is api/analytics_simple.py (/api/analytics/*)."""
    raise _NOT_IMPLEMENTED

# Summarization Templates — NOT IMPLEMENTED. The old handlers "persisted"
# templates to a process-local list (lost on every restart) and the frontend
# never calls these routes. 501 until a real DB-backed implementation lands.

@router.get("/summarization-templates")
async def get_templates(current_user: User = Depends(get_current_user)):
    """NOT IMPLEMENTED — no template storage exists."""
    raise _NOT_IMPLEMENTED

@router.post("/summarization-templates")
async def create_template(current_user: User = Depends(get_current_user)):
    """NOT IMPLEMENTED — previously wrote to a process-local list."""
    raise _NOT_IMPLEMENTED

@router.put("/summarization-templates/{template_id}")
async def update_template(
    template_id: str,
    current_user: User = Depends(get_current_user),
):
    """NOT IMPLEMENTED — previously wrote to a process-local list."""
    raise _NOT_IMPLEMENTED

@router.delete("/summarization-templates/{template_id}")
async def delete_template(
    template_id: str,
    current_user: User = Depends(get_current_user),
):
    """NOT IMPLEMENTED — previously wrote to a process-local list."""
    raise _NOT_IMPLEMENTED

# Helper Functions
def format_srt_time(seconds: float) -> str:
    """Format seconds to SRT timestamp"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
