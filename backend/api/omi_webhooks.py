"""
Omi integration — real webhook-based ingestion (docs.omi.me/doc/developer/apps/Integrations).

Omi (Based Hardware) is a webhook PUSH model, not something we poll or
connect to over a persistent socket: the user installs an "Integration App"
in their Omi app and pastes a URL for one or both of:

  * Real-Time Transcript webhook — fires repeatedly as a live conversation
    is transcribed: POST <url>?session_id=...&uid=... with
    {"session_id": ..., "segments": [{"text", "speaker", "is_user", ...}]}
  * Memory Creation webhook — fires once when Omi finalizes a conversation
    ("memory"): POST <url>?uid=... with the full transcript + Omi's own
    structured summary/action items + metadata.

Two platform constraints shaped this module's auth design (verified against
Omi's own docs and a known, open Omi platform bug — BasedHardware/omi#11365 —
where the app appends `?uid=` instead of `&uid=` whenever the configured
webhook URL already contains a `?`):

  1. Omi cannot be configured with custom headers, so a bearer token in an
     `Authorization` header (as api/stable_ingest.py uses for the analogous
     machine-to-machine ingestion contract) is not available to us here.
  2. Any query string we put in the *configured* webhook URL collides with
     the `?session_id=...&uid=...`/`?uid=...` Omi appends, so a `?token=...`
     scheme is unreliable.

We therefore carry the integration credential in the URL PATH instead
(`/omi/{token}/...`), so Omi's own appended query string is the request's
only `?`, sidestepping the bug entirely. The token is a Personal Access
Token with a dedicated `omi.transcript.ingest` scope (see
api/personal_access_tokens.py) bound to exactly one organization — the same
hash-and-compare mechanism used everywhere else in this codebase, just
carried differently for this one integration.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from auth.models import Organization, PersonalAccessToken, User, UserOrganization
from auth.pat import TOKEN_PREFIX as PAT_PREFIX, resolve_pat_record
from auth.utils import check_permission
from database.database import get_db
from database.models import RecordingSession, Transcription

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/integrations/omi", tags=["omi-integration"])

OMI_INGEST_SCOPE = "omi.transcript.ingest"
# RecordingSession.mode is constrained (ck_recording_sessions_mode) to
# 'upload' | 'live' | 'always_on'. A live, in-progress Omi capture is
# genuinely a live session; a fully-formed memory delivered with no prior
# real-time leg is modeled the same way api/stable_ingest.py models Stable
# imports — as a completed 'upload'-mode session, since Omi provenance is
# already explicit via source_type/external_source.
OMI_LIVE_MODE = "live"
OMI_BATCH_MODE = "upload"
OMI_SOURCE = "omi"


# ---------------------------------------------------------------------------
# Auth: resolve the {token} path segment to an org-bound principal
# ---------------------------------------------------------------------------
@dataclass
class OmiIngestPrincipal:
    user: User
    token: PersonalAccessToken
    organization: Organization


def resolve_omi_principal(token: str, db: Session) -> OmiIngestPrincipal:
    if not token or not token.startswith(PAT_PREFIX):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Omi integration token")
    pat = resolve_pat_record(db, plaintext=token)
    if pat is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid, expired, or revoked integration token")
    if pat.scope != OMI_INGEST_SCOPE or pat.organization_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token is not authorized for Omi ingestion")

    organization = (
        db.query(Organization)
        .filter(Organization.id == pat.organization_id, Organization.is_active.is_(True))
        .first()
    )
    if organization is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    user = pat.user
    membership = (
        db.query(UserOrganization)
        .filter(UserOrganization.user_id == user.id, UserOrganization.organization_id == organization.id)
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this organization")
    role = "superuser" if user.is_superuser else membership.role
    if not check_permission(role, "session.create"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to create sessions in this organization")

    return OmiIngestPrincipal(user=user, token=pat, organization=organization)


# ---------------------------------------------------------------------------
# Real-Time Transcript webhook payload (per docs.omi.me/doc/developer/apps/Integrations)
# ---------------------------------------------------------------------------
class OmiTranscriptSegment(BaseModel):
    text: str = Field(min_length=0, max_length=8000)
    speaker: Optional[str] = Field(default=None, max_length=256)
    is_user: bool = False
    start: Optional[float] = None
    end: Optional[float] = None


class OmiRealtimeTranscriptPayload(BaseModel):
    session_id: str = Field(min_length=1, max_length=256)
    segments: list[OmiTranscriptSegment] = Field(default_factory=list, max_length=500)


def _find_or_create_live_session(
    db: Session,
    *,
    principal: OmiIngestPrincipal,
    omi_session_id: str,
) -> RecordingSession:
    row = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.organization_id == principal.organization.id,
            RecordingSession.external_source == OMI_SOURCE,
            RecordingSession.external_id == omi_session_id,
        )
        .first()
    )
    if row is not None:
        return row

    now = datetime.now(timezone.utc)
    row = RecordingSession(
        name=f"Omi live session {omi_session_id[:8]}",
        title=f"Omi live session {omi_session_id[:8]}",
        description="Live capture ingested from an Omi Real-Time Transcript webhook.",
        status="active",
        mode=OMI_LIVE_MODE,
        created_at=now,
        started_at=now,
        transcript="",
        transcript_simple="",
        transcript_diarized={"text": "", "segments": [], "speakers": [], "source": OMI_SOURCE},
        user_id=principal.user.id,
        organization_id=principal.organization.id,
        source_type=OMI_SOURCE,
        external_source=OMI_SOURCE,
        external_id=omi_session_id,
        processing_metadata={"omi": {"ingest_principal_pat_id": principal.token.id, "realtime_started_at": now.isoformat()}},
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        row = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.organization_id == principal.organization.id,
                RecordingSession.external_source == OMI_SOURCE,
                RecordingSession.external_id == omi_session_id,
            )
            .first()
        )
        if row is None:
            raise
    return row


async def _broadcast_live_transcript(session: RecordingSession, new_segments: list[dict]) -> None:
    """Best-effort push to the existing live-transcription websocket layer so
    Omi-sourced speech shows up in the Live Meeting UI exactly like
    browser-mic capture does. Never fails the webhook if the websocket
    manager isn't loaded (e.g. under test)."""
    try:
        from api.websocket_transcription import manager as ws_manager  # type: ignore

        await ws_manager.broadcast(
            session.session_id or str(session.id),
            {
                "type": "transcript_update",
                "source": OMI_SOURCE,
                "session_id": session.session_id or str(session.id),
                "segments": new_segments,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Omi realtime: live websocket broadcast skipped (%s)", exc)


@router.post("/{token}/realtime-transcript", status_code=status.HTTP_200_OK)
async def omi_realtime_transcript(
    token: str = Path(...),
    body: OmiRealtimeTranscriptPayload = ...,
    uid: Optional[str] = Query(default=None),
    session_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Process live transcript segments as Omi produces them.

    Idempotent per-segment append: each incoming segment is hashed
    (session+text+start+end) and skipped if already stored, since Omi may
    redeliver the tail of a session on reconnect.
    """
    principal = resolve_omi_principal(token, db)
    omi_session_id = body.session_id or session_id
    if not omi_session_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="session_id is required")

    row = _find_or_create_live_session(db, principal=principal, omi_session_id=omi_session_id)

    diarized = row.transcript_diarized if isinstance(row.transcript_diarized, dict) else {"segments": [], "speakers": []}
    existing_segments = diarized.get("segments") or []
    existing_hashes = {s.get("_dedup_hash") for s in existing_segments if s.get("_dedup_hash")}

    new_segments: list[dict] = []
    speakers = list(diarized.get("speakers") or [])
    for seg in body.segments:
        text = seg.text.strip()
        if not text:
            continue
        dedup_hash = hashlib.sha256(
            f"{omi_session_id}|{seg.speaker}|{seg.start}|{seg.end}|{text}".encode("utf-8")
        ).hexdigest()
        if dedup_hash in existing_hashes:
            continue
        speaker_label = seg.speaker or ("You" if seg.is_user else "Speaker")
        if speaker_label not in speakers:
            speakers.append(speaker_label)
        segment = {
            "start": seg.start if seg.start is not None else 0.0,
            "end": seg.end if seg.end is not None else (seg.start or 0.0),
            "text": text,
            "speaker": speaker_label,
            "is_user": seg.is_user,
            "_dedup_hash": dedup_hash,
        }
        existing_segments.append(segment)
        new_segments.append(segment)
        existing_hashes.add(dedup_hash)
        db.add(
            Transcription(
                session_id=row.id,
                text=text,
                speaker=speaker_label,
                start_time=segment["start"],
                end_time=segment["end"],
                confidence=None,
            )
        )

    if new_segments:
        plain_text = "\n".join(f"{s['speaker']}: {s['text']}" for s in existing_segments)
        row.transcript_diarized = {
            "text": plain_text,
            "segments": existing_segments,
            "speakers": speakers,
            "source": OMI_SOURCE,
        }
        row.transcript = plain_text
        row.transcript_simple = plain_text
        row.speaker_count = len(speakers)
        flag_modified(row, "transcript_diarized")
        db.commit()
        await _broadcast_live_transcript(row, new_segments)
    else:
        db.commit()

    return {"ok": True, "session_id": row.session_id or str(row.id), "segments_received": len(body.segments), "segments_new": len(new_segments)}


# ---------------------------------------------------------------------------
# Memory Creation webhook: finalizes a conversation and runs it through the
# SAME finalize pipeline api/uploads.py uses (summarize -> insights ->
# Qdrant index), so an Omi-sourced meeting is not a second-class citizen.
# ---------------------------------------------------------------------------
class OmiMemoryTranscriptSegment(BaseModel):
    text: str = Field(min_length=0, max_length=8000)
    speaker: Optional[str] = Field(default=None, max_length=256)
    is_user: bool = False
    start: Optional[float] = None
    end: Optional[float] = None


class OmiMemoryPayload(BaseModel):
    id: str = Field(min_length=1, max_length=256, description="Omi's conversation/memory id — used for idempotent dedup")
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    transcript_segments: list[OmiMemoryTranscriptSegment] = Field(default_factory=list, max_length=5000)
    # Omi's own structured output, when present. We DO NOT treat this as
    # authoritative — the acceptance test requires action items/decisions to
    # come from OUR grounded extraction (Lyzr + our Qdrant context) so they
    # are traceable to segments the same way locally-transcribed meetings
    # are. Omi's structured payload is stored for reference only.
    structured: dict[str, Any] = Field(default_factory=dict)


class OmiMemoryResponse(BaseModel):
    ok: bool = True
    session_id: str
    created: bool
    segments_indexed: int


async def _finalize_omi_session(db: Session, session: RecordingSession) -> None:
    """Run the same summarize -> AI-insights -> Qdrant-index sequence
    api/uploads.py's run_upload_pipeline runs after transcription — reused,
    not reimplemented, so Omi-sourced meetings get identical treatment."""
    from api.uploads import _summarize_session  # local import: avoid import cycle at module load

    try:
        await _summarize_session(db, session, template="standard")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Omi finalize: summarization failed for session=%s: %s", session.id, exc)

    try:
        from api.ai_insights import _generate_ai_insights

        transcriptions = (
            db.query(Transcription)
            .filter(Transcription.session_id == session.id)
            .order_by(Transcription.start_time.asc())
            .all()
        )
        full_text = (session.transcript_simple or session.transcript or "").strip()
        insights = await _generate_ai_insights(full_text, transcriptions, session, db, session.organization_id)
        session.ai_insights = insights.model_dump()
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Omi finalize: AI insights failed for session=%s: %s", session.id, exc)

    try:
        from services.semantic_search_service import semantic_search
        import asyncio

        segs = session.transcript_diarized.get("segments") if isinstance(session.transcript_diarized, dict) else None
        index_transcript = (
            "\n".join(f"{(s.get('speaker') or 'Speaker')}: {(s.get('text') or '').strip()}" for s in segs if (s.get("text") or "").strip())
            if segs
            else (session.transcript_simple or session.transcript or "")
        )
        index_summary = session.summary or ""
        await asyncio.to_thread(
            semantic_search.index_session,
            session_id=session.session_id or str(session.id),
            title=session.title or session.name or "",
            transcript=index_transcript,
            summary=index_summary,
            created_at=session.created_at.isoformat() if session.created_at else "",
            organization_id=session.organization_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Omi finalize: Qdrant indexing failed for session=%s: %s", session.id, exc)
        meta = session.processing_metadata if isinstance(session.processing_metadata, dict) else {}
        meta["needs_index_retry"] = True
        session.processing_metadata = meta
        flag_modified(session, "processing_metadata")
        db.commit()


@router.post("/{token}/memory-created", response_model=OmiMemoryResponse, status_code=status.HTTP_200_OK)
async def omi_memory_created(
    token: str = Path(...),
    body: OmiMemoryPayload = ...,
    uid: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Finalize a completed Omi 'memory' (conversation) into a Meeting-Ops
    session and run it through the real summarize/extract/index pipeline."""
    principal = resolve_omi_principal(token, db)

    existing = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.organization_id == principal.organization.id,
            RecordingSession.external_source == OMI_SOURCE,
            RecordingSession.external_id == body.id,
        )
        .first()
    )

    segments = []
    speakers: list[str] = []
    for seg in body.transcript_segments:
        text = seg.text.strip()
        if not text:
            continue
        speaker_label = seg.speaker or ("You" if seg.is_user else "Speaker")
        if speaker_label not in speakers:
            speakers.append(speaker_label)
        segments.append(
            {
                "start": seg.start if seg.start is not None else 0.0,
                "end": seg.end if seg.end is not None else (seg.start or 0.0),
                "text": text,
                "speaker": speaker_label,
                "is_user": seg.is_user,
            }
        )
    plain_text = "\n".join(f"{s['speaker']}: {s['text']}" for s in segments)
    started = body.started_at or body.created_at or datetime.now(timezone.utc)
    finished = body.finished_at or started

    if existing is not None:
        session = existing
        session.transcript_diarized = {"text": plain_text, "segments": segments, "speakers": speakers, "source": OMI_SOURCE}
        session.transcript = plain_text
        session.transcript_simple = plain_text
        session.speaker_count = len(speakers)
        session.status = "completed"
        session.ended_at = finished
        session.duration = max(0.0, (finished - started).total_seconds())
        flag_modified(session, "transcript_diarized")
        db.query(Transcription).filter(Transcription.session_id == session.id).delete()
        for seg in segments:
            db.add(Transcription(session_id=session.id, text=seg["text"], speaker=seg["speaker"], start_time=seg["start"], end_time=seg["end"], confidence=None))
        db.commit()
        created = False
    else:
        session = RecordingSession(
            name=f"Omi memory {body.id[:8]}",
            title=f"Omi memory {body.id[:8]}",
            description="Conversation finalized by Omi (Memory Creation webhook).",
            status="completed",
            mode=OMI_BATCH_MODE,
            created_at=body.created_at or datetime.now(timezone.utc),
            started_at=started,
            ended_at=finished,
            meeting_date=started.date(),
            duration=max(0.0, (finished - started).total_seconds()),
            transcript=plain_text,
            transcript_simple=plain_text,
            transcript_diarized={"text": plain_text, "segments": segments, "speakers": speakers, "source": OMI_SOURCE},
            speaker_count=len(speakers),
            user_id=principal.user.id,
            organization_id=principal.organization.id,
            source_type=OMI_SOURCE,
            external_source=OMI_SOURCE,
            external_id=body.id,
            extra_data={"omi_structured": body.structured} if body.structured else None,
            processing_metadata={"omi": {"ingest_principal_pat_id": principal.token.id}},
        )
        db.add(session)
        try:
            db.flush()
            for seg in segments:
                db.add(Transcription(session_id=session.id, text=seg["text"], speaker=seg["speaker"], start_time=seg["start"], end_time=seg["end"], confidence=None))
            db.commit()
            db.refresh(session)
            created = True
        except IntegrityError:
            db.rollback()
            session = (
                db.query(RecordingSession)
                .filter(
                    RecordingSession.organization_id == principal.organization.id,
                    RecordingSession.external_source == OMI_SOURCE,
                    RecordingSession.external_id == body.id,
                )
                .first()
            )
            if session is None:
                raise
            created = False

    await _finalize_omi_session(db, session)

    return OmiMemoryResponse(ok=True, session_id=session.session_id or str(session.id), created=created, segments_indexed=len(segments))
