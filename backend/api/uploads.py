"""Chunked media uploads and pre-recorded meeting processing."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import shutil
import uuid
from contextlib import suppress
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy import Text
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from auth.tier import gate_feature_for_caller
from auth.ws_auth import enforce_ws_auth
from database.database import SessionLocal, get_db
from database.models import RecordingSession, Transcription, UploadJob

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/uploads", tags=["Uploads"])
ws_router = APIRouter(tags=["Uploads"])

DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024
# Host-wide fallback ceiling when UPLOAD_MAX_FILE_BYTES is unset. Kept at/above
# the largest tier cap (enterprise = 10 GB, see services/quotas.py) so a fresh
# deploy that forgets the env can't silently cap paid tiers. 16 GB = headroom
# for full video-meeting recordings; per-org tier quota still gates below this.
DEFAULT_MAX_FILE_BYTES = 17179869184
RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "/app/recordings"))
UPLOAD_ROOT = RECORDINGS_DIR / "uploads"

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".opus", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
ATTACHABLE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".docx", ".txt", ".md"}
PIPELINE_STAGES = ["queued", "uploading", "extracting", "transcribing", "diarizing", "summarizing", "embedding", "done"]
RECOVERABLE_PIPELINE_STAGES = {
    "queued", "extracting", "transcribing", "diarizing", "summarizing", "embedding",
}


class UploadStartRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    total_size: int = Field(gt=0)
    content_type: Optional[str] = None
    action: str
    target_session_id: Optional[str] = None
    # Per-upload pipeline overrides — falls back to org defaults when missing.
    # Schema: services/transcription_options.TranscriptionOptions.
    transcription_options: Optional[dict] = None
    # Browser File.lastModified as an ISO-8601 string. Best-effort "file date"
    # used to default meeting_date; silently ignored if unparseable. (Browsers
    # can't expose an OS creation date, so last-modified is the only signal.)
    client_modified_at: Optional[str] = None


class UploadStartResponse(BaseModel):
    upload_id: str
    chunk_size: int
    max_file_bytes: int
    total_chunks: int
    stage: str


class UploadFinalizeResponse(BaseModel):
    job_id: str
    session_id: Optional[int] = None
    # Set when this audio is byte-identical to an already-completed session in
    # the same org. The client should send the user to `session_id` rather than
    # showing progress; re-POST finalize with force=true to process anyway.
    is_duplicate: bool = False
    duplicate_message: Optional[str] = None


class UploadStatusResponse(BaseModel):
    upload_id: str
    job_id: str
    filename: str
    stage: str
    progress_pct: int
    error: Optional[str] = None
    session_id: Optional[int] = None
    bytes_received: int
    total_size: int
    received_chunks: int
    total_chunks: int
    last_completed_stage: Optional[str] = None
    retry_count: int = 0


class UploadWebSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}

    async def connect(self, upload_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(upload_id, set()).add(websocket)

    def disconnect(self, upload_id: str, websocket: WebSocket) -> None:
        conns = self._connections.get(upload_id)
        if not conns:
            return
        conns.discard(websocket)
        if not conns:
            self._connections.pop(upload_id, None)

    async def broadcast(self, upload_id: str, payload: dict[str, Any]) -> None:
        conns = list(self._connections.get(upload_id, set()))
        for websocket in conns:
            try:
                await websocket.send_json(payload)
            except Exception:
                self.disconnect(upload_id, websocket)


ws_manager = UploadWebSocketManager()


def get_max_file_bytes() -> int:
    try:
        return int(os.getenv("UPLOAD_MAX_FILE_BYTES", str(DEFAULT_MAX_FILE_BYTES)))
    except ValueError:
        return DEFAULT_MAX_FILE_BYTES


def get_pipeline_concurrency() -> int:
    try:
        return max(1, int(os.getenv("UPLOAD_PIPELINE_CONCURRENCY", "4")))
    except ValueError:
        return 4


def _safe_filename(filename: str) -> str:
    name = Path(filename).name.strip().replace("\x00", "")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name)
    return name[:240] or "upload.bin"


def _job_dir(org_slug: str, upload_id: str) -> Path:
    return UPLOAD_ROOT / org_slug / upload_id


def _detect_kind(filename: str, content_type: Optional[str]) -> str:
    ext = Path(filename).suffix.lower()
    ctype = (content_type or "").lower()
    if ctype.startswith("audio/") or ext in AUDIO_EXTENSIONS:
        return "audio"
    if ctype.startswith("video/") or ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in ATTACHABLE_EXTENSIONS:
        return "attachable"
    return "unknown"


def _title_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    stem = re.sub(r"^(zoom|recording|meeting|audio|video)[ _-]+", "", stem, flags=re.I)
    stem = re.sub(r"(^|[ _-])\d{4}[-_]\d{2}[-_]\d{2}([ _-]|$)", " ", stem)
    stem = re.sub(r"\b\d{8}\b", "", stem)
    words = re.sub(r"[_-]+", " ", stem).strip()
    if not words:
        words = "Uploaded Meeting"
    titled = " ".join(part.capitalize() for part in words.split())
    return titled[:80]


def _created_at_from_filename_or_mtime(path: Path, fallback: datetime) -> datetime:
    match = re.search(r"(\d{4})[-_](\d{2})[-_](\d{2})", path.name)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return fallback


def _flag_stage_needs_retry(session, stage: str, exc: Exception) -> None:
    """Record a degraded pipeline stage so the upload COMPLETES with what
    succeeded, while marking the stage for retry/visibility instead of silently
    finishing with missing data. Sets ``needs_<stage>`` + ``<stage>_error`` on
    processing_metadata (reassigned so SQLAlchemy detects the JSON change; the
    caller also flag_modifies + commits). Mirrors the always-on finalize path's
    degraded-stage markers, which the watchdog + a manual reprocess honor."""
    metadata = dict(session.processing_metadata or {})
    metadata[f"needs_{stage}"] = True
    metadata[f"{stage}_error"] = str(exc)[:500]
    session.processing_metadata = metadata


def _clear_stage_needs_retry(session, stage: str) -> None:
    """Clear a prior degraded-stage marker after a successful retry."""
    metadata = dict(session.processing_metadata or {})
    metadata.pop(f"needs_{stage}", None)
    metadata.pop(f"{stage}_error", None)
    session.processing_metadata = metadata


def _flag_summary_needs_retry(session, exc: Exception) -> None:
    """Summary-specific degraded-stage marker (``needs_summary`` /
    ``summary_error``). The session watchdog + a manual reprocess both honor it
    to regenerate the summary once the LLM is healthy."""
    _flag_stage_needs_retry(session, "summary", exc)


async def _set_job_state(
    db: Session,
    job: UploadJob,
    *,
    stage: Optional[str] = None,
    progress_pct: Optional[int] = None,
    error_message: Optional[str] = None,
    last_completed_stage: Optional[str] = None,
    session_id: Optional[int] = None,
    commit: bool = True,
) -> None:
    if stage is not None:
        job.stage = stage
    if progress_pct is not None:
        job.progress_pct = max(0, min(100, int(progress_pct)))
    if error_message is not None:
        job.error_message = error_message
    if last_completed_stage is not None:
        job.last_completed_stage = last_completed_stage
    if session_id is not None:
        job.session_id = session_id
    job.updated_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
        db.refresh(job)
    await ws_manager.broadcast(str(job.upload_id), _status_payload(job))


def _status_payload(job: UploadJob) -> dict[str, Any]:
    return {
        "upload_id": str(job.upload_id),
        "job_id": str(job.upload_id),
        "filename": job.filename,
        "stage": job.stage,
        "progress_pct": job.progress_pct or 0,
        "error": job.error_message,
        "session_id": job.session_id,
        "bytes_received": int(job.bytes_received or 0),
        "total_size": int(job.total_size or 0),
        "received_chunks": int(job.chunks_received or 0),
        "total_chunks": int(job.total_chunks or 0),
        "last_completed_stage": job.last_completed_stage,
        "retry_count": job.retry_count or 0,
    }


def _get_job(db: Session, upload_id: str, org_id: int) -> UploadJob:
    try:
        uid = uuid.UUID(upload_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Upload not found")
    job = db.query(UploadJob).filter(
        UploadJob.organization_id == org_id,
        UploadJob.upload_id == uid,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Upload not found")
    return job


class UploadPipelineQueue:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.workers: list[asyncio.Task] = []
        self.running: dict[str, asyncio.Task] = {}
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for idx in range(get_pipeline_concurrency()):
            self.workers.append(asyncio.create_task(self._worker(idx)))
        logger.info("Upload pipeline queue started with %s workers", len(self.workers))

    async def stop(self) -> None:
        for task in self.workers:
            task.cancel()
        for task in list(self.running.values()):
            task.cancel()
        if self.workers:
            await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        self.running.clear()
        self._started = False

    async def enqueue(self, upload_id: str) -> None:
        await self.queue.put(upload_id)

    def cancel(self, upload_id: str) -> None:
        task = self.running.get(upload_id)
        if task:
            task.cancel()

    async def _worker(self, idx: int) -> None:
        while True:
            upload_id = await self.queue.get()
            task = asyncio.current_task()
            if task:
                self.running[upload_id] = task
            try:
                await run_upload_pipeline(upload_id)
            except asyncio.CancelledError:
                await mark_upload_cancelled(upload_id)
            except Exception as exc:
                logger.exception("Upload worker %s failed for %s: %s", idx, upload_id, exc)
                await mark_upload_failed(upload_id, str(exc))
            finally:
                self.running.pop(upload_id, None)
                self.queue.task_done()


upload_pipeline_queue = UploadPipelineQueue()


async def start_upload_pipeline_queue() -> None:
    await upload_pipeline_queue.start()


async def stop_upload_pipeline_queue() -> None:
    await upload_pipeline_queue.stop()


async def recover_pending_uploads() -> int:
    """Re-enqueue durable upload jobs left in-flight by a process restart."""
    db = SessionLocal()
    recovered = 0
    try:
        jobs = db.query(UploadJob).filter(
            UploadJob.stage.in_(RECOVERABLE_PIPELINE_STAGES),
        ).order_by(UploadJob.created_at).all()
        for job in jobs:
            job_dir = _job_dir(_org_slug_for_job(db, job), str(job.upload_id))
            source_path = job_dir / job.filename
            extracted_path = job_dir / "extracted.wav"
            if not source_path.exists() and not extracted_path.exists():
                logger.warning(
                    "Upload %s remains %s but has no recoverable bytes",
                    job.upload_id,
                    job.stage,
                )
                continue
            await upload_pipeline_queue.enqueue(str(job.upload_id))
            recovered += 1
        if recovered:
            logger.info("Recovered %s pending upload jobs", recovered)
        return recovered
    finally:
        db.close()


async def cleanup_stale_uploads() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    db = SessionLocal()
    try:
        stale_uploading = db.query(UploadJob).filter(
            UploadJob.stage == "uploading",
            UploadJob.updated_at < cutoff,
        ).all()
        for job in stale_uploading:
            job.stage = "failed"
            job.error_message = "Upload expired before all chunks were received. Please retry."
            job.job_completed_at = datetime.now(timezone.utc)
        if stale_uploading:
            db.commit()

        # Never delete bytes for queued or in-process rows: startup recovery
        # needs them. Only terminal or now-expired incomplete uploads are safe.
        stale = db.query(UploadJob).filter(
            UploadJob.stage.in_(["failed", "cancelled"]),
            UploadJob.updated_at < cutoff,
        ).all()
        for job in stale:
            shutil.rmtree(_job_dir(_org_slug_for_job(db, job), str(job.upload_id)), ignore_errors=True)
        if stale:
            logger.info("Cleaned %s stale upload directories", len(stale))
    finally:
        db.close()


def _org_slug_for_job(db: Session, job: UploadJob) -> str:
    from auth.models import Organization
    org = db.query(Organization).filter(Organization.id == job.organization_id).first()
    return org.slug if org else str(job.organization_id)


@router.post("/start", response_model=UploadStartResponse)
async def start_upload(
    request: UploadStartRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if request.action not in {"transcribe", "attach"}:
        raise HTTPException(status_code=400, detail="action must be 'transcribe' or 'attach'.")

    if request.action == "transcribe":
        # v3.23.0 tier gate: switched from canonical_reprocess →
        # audio_upload. Basic tier ($7.99) gets server text storage +
        # corpus chat / search but NO audio upload (that's the line
        # between Basic and Pro). Pro/Suite/Enterprise still pass; Free
        # and Basic both 403 here.
        gate_feature_for_caller(current_user, "audio_upload", active_org)

    # Per-org tier quota check (file size, concurrent uploads, monthly hours).
    # Raises HTTPException(402/429) with a structured detail payload so the
    # frontend can show a useful upsell when the user hits a tier ceiling.
    from services.quotas import check_upload_quota
    check_upload_quota(db, active_org.organization, request.total_size)

    # Host-level hard cap (env-tunable). Enforces a global ceiling on top of
    # the tier limits — useful when an operator wants to keep total per-host
    # bandwidth bounded regardless of org tier.
    max_file_bytes = get_max_file_bytes()
    if request.total_size > max_file_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "message": f"File is larger than the configured upload limit of {max_file_bytes} bytes.",
                "max_file_bytes": max_file_bytes,
            },
        )

    kind = _detect_kind(request.filename, request.content_type)
    if request.action == "transcribe" and kind not in {"audio", "video"}:
        raise HTTPException(status_code=400, detail="Only audio and video files can be transcribed.")

    target_session = None
    if request.action == "attach":
        # v3.29.0 moat fix: an attach-upload runs the full server STT +
        # diarization + summary pipeline (run_upload_pipeline) just like a
        # transcribe-upload, so it must carry the same audio_upload gate
        # (Free/Basic 403). Previously only the "transcribe" branch gated.
        gate_feature_for_caller(current_user, "audio_upload", active_org)
        if not request.target_session_id:
            raise HTTPException(status_code=400, detail="target_session_id is required for attach uploads.")
        target_session = _find_session(db, request.target_session_id, active_org.organization.id)
        if not target_session:
            raise HTTPException(status_code=404, detail="Target session not found in active organization.")

    # Validate + normalize per-upload transcription options. Bad values
    # raise 422 with field-level detail (Pydantic). None means "use org
    # defaults", which the pipeline workers handle naturally.
    options_payload: Optional[dict] = None
    if request.transcription_options:
        from services.transcription_options import TranscriptionOptions
        options_payload = TranscriptionOptions(**request.transcription_options).to_jsonb()

    # Best-effort parse of the browser's File.lastModified. Never fatal — a bad
    # or absent value just means we fall back to the other recorded-at signals
    # (embedded creation_time > filename date > ... > upload time).
    client_modified_at = None
    if request.client_modified_at:
        from services.meeting_provenance import parse_iso
        client_modified_at = parse_iso(request.client_modified_at)

    upload_id = uuid.uuid4()
    total_chunks = max(1, math.ceil(request.total_size / DEFAULT_CHUNK_SIZE))
    job = UploadJob(
        organization_id=active_org.organization.id,
        user_id=current_user.id,
        upload_id=upload_id,
        filename=_safe_filename(request.filename),
        content_type=request.content_type,
        action=request.action,
        total_size=request.total_size,
        total_chunks=total_chunks,
        stage="uploading",
        progress_pct=0,
        session_id=target_session.id if target_session else None,
        transcription_options=options_payload,
        client_modified_at=client_modified_at,
    )
    db.add(job)
    db.commit()
    _job_dir(active_org.organization.slug, str(upload_id)).mkdir(parents=True, exist_ok=True)

    return UploadStartResponse(
        upload_id=str(upload_id),
        chunk_size=DEFAULT_CHUNK_SIZE,
        max_file_bytes=max_file_bytes,
        total_chunks=total_chunks,
        stage="uploading",
    )


@router.get("/quotas")
async def get_quota_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Snapshot of the active org's quota usage and tier limits.

    Returns the data the frontend needs to show usage bars + upsell prompts
    before a user even attempts an upload. Pure read: no quota enforcement
    happens here, only at /uploads/start.
    """
    from services.quotas import get_usage
    return get_usage(db, active_org.organization)


@router.post("/{upload_id}/chunk")
async def upload_chunk(
    upload_id: str,
    chunk_index: int = Form(...),
    data: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    job = _get_job(db, upload_id, active_org.organization.id)
    if job.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Upload belongs to another user.")
    if job.stage not in {"uploading", "queued"}:
        raise HTTPException(status_code=409, detail=f"Upload is already {job.stage}.")
    if chunk_index < 0 or chunk_index >= job.total_chunks:
        raise HTTPException(status_code=400, detail="chunk_index is out of range.")

    chunks_dir = _job_dir(active_org.organization.slug, upload_id) / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    target = chunks_dir / f"{chunk_index:08d}.part"
    body = await data.read()
    if not body:
        raise HTTPException(status_code=400, detail="Empty chunk.")
    target.write_bytes(body)

    files = list(chunks_dir.glob("*.part"))
    job.chunks_received = len(files)
    job.bytes_received = sum(p.stat().st_size for p in files)
    job.progress_pct = min(99, int((job.bytes_received / job.total_size) * 100))
    job.updated_at = datetime.now(timezone.utc)
    db.commit()
    await ws_manager.broadcast(upload_id, _status_payload(job))
    return {"received_chunks": job.chunks_received, "total_chunks": job.total_chunks}


async def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """SHA-256 of a file, off the event loop.

    Chunked so a multi-GB upload does not materialise in memory, and run in a
    thread so hashing does not stall the API worker.
    """

    def _digest() -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while True:
                block = fh.read(chunk_size)
                if not block:
                    break
                h.update(block)
        return h.hexdigest()

    return await asyncio.to_thread(_digest)


def _find_completed_session_by_sha256(
    db: Session, organization_id: int, sha256_hex: str
) -> Optional[RecordingSession]:
    """Existing COMPLETED session in this org holding byte-identical audio.

    Org-scoped on purpose, matching the bulk-import path: two orgs may
    legitimately upload the same audio (a podcast guest sharing with a host)
    and that must not leak across tenants.

    Only `completed` sessions count. A failed or still-processing session is
    NOT a duplicate -- returning one would strand the user on a broken record
    instead of letting them re-run it.

    Matched with a LIKE on the JSON-serialised column so this works on both
    Postgres JSONB and the SQLite test fixture, mirroring bulk_import_queue.
    The 64-char hex makes false positives effectively impossible.
    """
    needle = f'%"audio_sha256": "{sha256_hex}"%'
    return (
        db.query(RecordingSession)
        .filter(
            RecordingSession.organization_id == organization_id,
            RecordingSession.status == "completed",
            RecordingSession.processing_metadata.isnot(None),
            RecordingSession.processing_metadata.cast(Text).like(needle),
        )
        .order_by(RecordingSession.id.desc())
        .first()
    )


@router.post("/{upload_id}/finalize", response_model=UploadFinalizeResponse)
async def finalize_upload(
    upload_id: str,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    job = _get_job(db, upload_id, active_org.organization.id)
    if job.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Upload belongs to another user.")
    if job.chunks_received != job.total_chunks:
        raise HTTPException(status_code=400, detail="Not all chunks have been received.")

    base_dir = _job_dir(active_org.organization.slug, upload_id)
    chunks_dir = base_dir / "chunks"
    final_path = base_dir / job.filename
    with final_path.open("wb") as out:
        for idx in range(job.total_chunks):
            chunk = chunks_dir / f"{idx:08d}.part"
            if not chunk.exists():
                raise HTTPException(status_code=400, detail=f"Missing chunk {idx}.")
            with chunk.open("rb") as inp:
                shutil.copyfileobj(inp, out)

    actual_size = final_path.stat().st_size
    if actual_size != job.total_size:
        raise HTTPException(status_code=400, detail=f"Size mismatch: expected {job.total_size}, got {actual_size}.")

    # ---- SHA-256 + de-dupe, BEFORE any GPU work is queued ----
    # Only for `transcribe` (new session). `attach` deliberately binds audio to
    # a caller-chosen session, so a byte-identical file is the expected input
    # there and must not be intercepted.
    sha256_hex = await _sha256_file(final_path)
    job.audio_sha256 = sha256_hex
    if job.action == "transcribe" and not force:
        existing = _find_completed_session_by_sha256(
            db, active_org.organization.id, sha256_hex
        )
        if existing is not None:
            job.session_id = existing.id
            # "done" is the terminal UploadJob stage the frontend watches for
            # (see the pipeline's completion block). "completed" is the
            # RecordingSession status -- a different field on a different table.
            # Setting it here left the UI spinning on a stage that never occurs.
            job.stage = "done"
            job.progress_pct = 100
            job.last_completed_stage = "done"
            job.job_completed_at = datetime.now(timezone.utc)
            job.updated_at = datetime.now(timezone.utc)
            job.error_message = None
            db.commit()
            await ws_manager.broadcast(upload_id, _status_payload(job))
            logger.info(
                "upload %s is a duplicate of session %s (sha256=%s) - skipped processing",
                upload_id, existing.id, sha256_hex[:12],
            )
            return UploadFinalizeResponse(
                job_id=upload_id,
                session_id=existing.id,
                is_duplicate=True,
                duplicate_message=(
                    "This recording is already in your library. "
                    "Re-send with force=true to transcribe it again anyway."
                ),
            )

    # Both transcribe (new session) and attach (existing session) flow through
    # the same processing pipeline; run_upload_pipeline branches on job.action
    # to either create a new RecordingSession or reuse the target one.
    job.stage = "queued"
    job.progress_pct = 0
    job.error_message = None
    job.job_started_at = None
    db.commit()
    await ws_manager.broadcast(upload_id, _status_payload(job))
    await upload_pipeline_queue.enqueue(upload_id)
    return UploadFinalizeResponse(job_id=upload_id, session_id=job.session_id)


@router.get("/{upload_id}/status", response_model=UploadStatusResponse)
async def get_upload_status(
    upload_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    job = _get_job(db, upload_id, active_org.organization.id)
    if job.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Upload belongs to another user.")
    return UploadStatusResponse(**_status_payload(job))


@router.delete("/{upload_id}")
async def cancel_upload(
    upload_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    job = _get_job(db, upload_id, active_org.organization.id)
    if job.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Upload belongs to another user.")
    upload_pipeline_queue.cancel(upload_id)
    job.stage = "cancelled"
    job.progress_pct = 0
    job.job_completed_at = datetime.now(timezone.utc)
    job.updated_at = datetime.now(timezone.utc)
    if job.session_id:
        session = db.query(RecordingSession).filter(
            RecordingSession.id == job.session_id,
            RecordingSession.organization_id == active_org.organization.id,
        ).first()
        if session and session.status != "completed":
            db.query(Transcription).filter(Transcription.session_id == session.id).delete()
            db.delete(session)
            job.session_id = None
    db.commit()
    shutil.rmtree(_job_dir(active_org.organization.slug, upload_id), ignore_errors=True)
    await ws_manager.broadcast(upload_id, _status_payload(job))
    return {"status": "cancelled"}


MAX_UPLOAD_RETRIES = int(os.getenv("UPLOAD_MAX_RETRIES", "3"))


@router.post("/{upload_id}/retry", response_model=UploadStatusResponse)
async def retry_upload(
    upload_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Re-queue a failed or cancelled upload through the processing pipeline.

    The chunked file bytes still live on disk under uploads/<org>/<upload_id>/
    until cancellation tears them down. Retry skips the upload phase entirely
    and re-runs extraction → transcription → diarization → summary → embed.
    Capped at UPLOAD_MAX_RETRIES (default 3); the cap can be raised by a
    superuser via the ?force=true query param.
    """
    job = _get_job(db, upload_id, active_org.organization.id)
    if job.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Upload belongs to another user.")

    if job.stage not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=409,
            detail=f"Upload is in stage '{job.stage}'; only failed or cancelled uploads can be retried.",
        )

    if (job.retry_count or 0) >= MAX_UPLOAD_RETRIES and not current_user.is_superuser:
        raise HTTPException(
            status_code=429,
            detail=f"Upload has been retried {job.retry_count} times (cap {MAX_UPLOAD_RETRIES}). "
            "Contact an administrator to retry further.",
        )

    job_dir = _job_dir(active_org.organization.slug, upload_id)
    if not job_dir.exists():
        raise HTTPException(
            status_code=410,
            detail="Upload bytes are no longer on disk; please re-upload.",
        )

    job.stage = "queued"
    job.progress_pct = 0
    job.error_message = None
    job.last_completed_stage = None
    job.retry_count = (job.retry_count or 0) + 1
    job.job_started_at = None
    job.job_completed_at = None
    job.updated_at = datetime.now(timezone.utc)
    db.commit()

    await ws_manager.broadcast(upload_id, _status_payload(job))
    await upload_pipeline_queue.enqueue(upload_id)

    logger.info(
        f"Upload {upload_id} retry #{job.retry_count} enqueued by user {current_user.id} (org {active_org.organization.slug})"
    )
    return UploadStatusResponse(**_status_payload(job))


class ReprocessRequest(BaseModel):
    """Body for the reprocess endpoint — same options as a fresh upload."""
    transcription_options: Optional[dict] = None


@router.post("/sessions/{session_id}/reprocess", response_model=UploadStatusResponse)
async def reprocess_session(
    session_id: str,
    request: ReprocessRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Re-run the transcription / diarization / summarization / embedding
    pipeline against an existing session's audio file.

    Reuses the chunked-upload UploadJob shape so the frontend's existing
    progress + retry tray surfaces the work uniformly. The audio bytes are
    NOT re-uploaded — we point a synthetic UploadJob at the session's
    existing audio path on disk.

    Accepts either the UUID `session_id` column or the integer primary key
    so callers can use whichever they already have in hand. Mirrors the
    lookup pattern used by ai_chat and other session-scoped endpoints.
    """
    gate_feature_for_caller(current_user, "canonical_reprocess", active_org)  # v3.29.0 moat fix: reprocess runs the paid server pipeline
    rec = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.session_id == session_id,
            RecordingSession.organization_id == active_org.organization.id,
        )
        .first()
    )
    if not rec:
        # Fall back to integer primary key lookup for older frontend paths
        # that pass the numeric id.
        try:
            pk = int(session_id)
        except (TypeError, ValueError):
            pk = None
        if pk is not None:
            rec = (
                db.query(RecordingSession)
                .filter(
                    RecordingSession.id == pk,
                    RecordingSession.organization_id == active_org.organization.id,
                )
                .first()
            )
    if not rec:
        # Fallback: the session may live in a DIFFERENT org while the caller
        # still has access (e.g. a personal-org recording viewed while active
        # in another org, or a per-meeting collaborator). Same resolution the
        # rediarize/speakers endpoints use — resolve cross-org, then delegate
        # the permission decision to has_session_access.
        from api.session_permissions import _get_session_by_str_id, has_session_access

        candidate = _get_session_by_str_id(db, str(session_id))
        if candidate and has_session_access(candidate.id, current_user, db) != "denied":
            rec = candidate
    if not rec:
        raise HTTPException(status_code=404, detail="Session not found in active organization.")
    # Everything session-scoped below (chunk dirs, the synthetic UploadJob, and
    # the worker's own org-strict session lookup) must use the SESSION'S org —
    # which for cross-org sessions is not the caller's active org. Billing/gating
    # above stays on the caller's active org by design.
    from auth.models import Organization

    sess_org = (
        active_org.organization
        if rec.organization_id == active_org.organization.id
        else db.query(Organization).filter(Organization.id == rec.organization_id).first()
    )
    if not sess_org:
        raise HTTPException(status_code=404, detail="Session's organization not found.")
    # Resolve the source audio. Always-on / browser recordings keep their audio
    # as ~30s chunks on disk; the reassembled `audio_file` wav may be missing
    # (evicted to Garage) OR stale (the pre-v3.47 truncated 30s copy). When chunks
    # exist, reassemble them (byte-concat — the canonical full audio) so the manual
    # Reprocess button works for those sessions and re-runs over the FULL meeting,
    # not a stale/truncated wav. Uploaded files (no chunks) use their stored wav.
    from api.recording import _audio_chunks_dir, _list_chunk_files, _reassemble_full_audio
    _canonical_sid = rec.session_id or str(rec.id)
    _chunks_dir = _audio_chunks_dir(sess_org.slug, _canonical_sid)
    if _list_chunk_files(_chunks_dir):
        _target_wav = _chunks_dir.parent / "session.wav"
        try:
            await _reassemble_full_audio(_chunks_dir, _target_wav)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Audio reassembly failed: {exc}")
        rec.audio_file = str(_target_wav)
        db.commit()
    elif not rec.audio_file or not Path(rec.audio_file).exists():
        raise HTTPException(
            status_code=410,
            detail="No recoverable audio for this session (no chunks on disk and no stored audio file).",
        )

    # Invalidate the cached ai_insights so the next /insights GET re-extracts
    # against the new transcript/diarization. The pipeline also overwrites
    # summary fields when it completes, but ai_insights lives separately.
    rec.ai_insights = None
    db.commit()

    # Validate options early.
    options_payload: Optional[dict] = None
    if request.transcription_options:
        from services.transcription_options import TranscriptionOptions
        options_payload = TranscriptionOptions(**request.transcription_options).to_jsonb()

    # Park the existing audio under a fresh upload_id directory so the
    # pipeline's source-of-truth path matches the chunked flow's layout.
    job_id = uuid.uuid4()
    job_dir = _job_dir(sess_org.slug, str(job_id))
    job_dir.mkdir(parents=True, exist_ok=True)
    src_audio = Path(rec.audio_file)
    parked = job_dir / src_audio.name
    if not parked.exists():
        try:
            os.symlink(src_audio.resolve(), parked)
        except OSError:
            shutil.copy2(src_audio, parked)

    job = UploadJob(
        organization_id=rec.organization_id,
        user_id=current_user.id,
        upload_id=job_id,
        filename=src_audio.name,
        content_type="application/octet-stream",
        action="attach",
        total_size=src_audio.stat().st_size,
        bytes_received=src_audio.stat().st_size,
        chunks_received=1,
        total_chunks=1,
        stage="queued",
        progress_pct=0,
        session_id=rec.id,
        transcription_options=options_payload,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    await ws_manager.broadcast(str(job_id), _status_payload(job))
    await upload_pipeline_queue.enqueue(str(job_id))
    logger.info(
        f"Reprocess enqueued for session {session_id} (job {job_id}) by user {current_user.id} "
        f"(session org {sess_org.slug}, active org {active_org.organization.slug}) "
        f"with options={options_payload}"
    )
    return UploadStatusResponse(**_status_payload(job))


async def mark_upload_cancelled(upload_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(UploadJob).filter(UploadJob.upload_id == uuid.UUID(upload_id)).first()
        if job:
            job.stage = "cancelled"
            job.job_completed_at = datetime.now(timezone.utc)
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            await ws_manager.broadcast(upload_id, _status_payload(job))
    finally:
        db.close()


async def mark_upload_failed(upload_id: str, error: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(UploadJob).filter(UploadJob.upload_id == uuid.UUID(upload_id)).first()
        if job:
            job.stage = "failed"
            job.error_message = error[:2000]
            job.job_completed_at = datetime.now(timezone.utc)
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            await ws_manager.broadcast(upload_id, _status_payload(job))
    finally:
        db.close()


def _stamp_speaker_contacts_fresh(session_pk: int) -> None:
    """Run the confirmed speaker->contact participant stamp in its OWN fresh
    DB session (re-querying the row) so it never clobbers a concurrently
    committed status/participant write. Best-effort."""
    from services.speaker_service import stamp_confirmed_speaker_contacts

    db = SessionLocal()
    try:
        s = (
            db.query(RecordingSession)
            .filter(RecordingSession.id == session_pk)
            .first()
        )
        if s is not None:
            stamp_confirmed_speaker_contacts(s, db)
    finally:
        db.close()


async def run_upload_pipeline(upload_id: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(UploadJob).filter(UploadJob.upload_id == uuid.UUID(upload_id)).first()
        if not job or job.stage == "cancelled":
            return
        org_slug = _org_slug_for_job(db, job)
        base_dir = _job_dir(org_slug, upload_id)
        source_path = base_dir / job.filename
        if not source_path.exists() or source_path.stat().st_size == 0:
            raise RuntimeError("Uploaded file is missing or empty.")
        if source_path.stat().st_size > get_max_file_bytes():
            raise RuntimeError(f"File exceeds upload limit of {get_max_file_bytes()} bytes.")

        job.job_started_at = datetime.now(timezone.utc)
        await _set_job_state(db, job, stage="queued", progress_pct=1, commit=True)

        media_kind = _detect_kind(job.filename, job.content_type)
        if media_kind not in {"audio", "video"}:
            raise RuntimeError("Unsupported media format for transcription.")

        # Always normalize to 16 kHz mono PCM WAV before handing off to the
        # STT provider. whisper.cpp's HTTP server can decode m4a/mp3 internally
        # but it is strictly faster + more reliable on a pre-extracted WAV,
        # and the wav file size gives the timeout heuristic an accurate
        # signal (compressed audio MB/min lies about real duration). The
        # extra ffmpeg pass is cheap (~1.4 kx realtime in practice).
        audio_path = base_dir / "extracted.wav"
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            await _set_job_state(db, job, stage="extracting", progress_pct=3)
            await _extract_audio(source_path, audio_path, upload_id, db, job)
            await _set_job_state(db, job, progress_pct=100, last_completed_stage="extracting")

        # Materialize per-upload preferences. Empty / None object = use the
        # active org's saved settings end-to-end.
        from services.transcription_options import coerce_options
        opts = coerce_options(job.transcription_options)

        session = await _resolve_session_for_job(db, job, audio_path)

        # --- Kick off diarization CONCURRENTLY with transcription. STT
        # (Parakeet) and diarization (pyannote) read the same audio
        # independently on different GPUs and are only merged after BOTH
        # finish, so running them serially just wastes wall-clock. Set up the
        # provider (a sync DB read) and launch diarize() here, then await it in
        # the diarization stage below. diarize() is HTTP-only (no DB access) so
        # it can't race the shared SQLAlchemy session that _transcribe_audio /
        # _set_job_state use. Any setup error degrades cleanly to a fresh
        # synchronous call in the stage below (diarize_task stays None). Mirrors
        # the always-on reprocess path (_run_session_reprocess).
        diarize_task = None
        diarization_off = (opts.diarization_mode or "auto") == "off"
        if not diarization_off:
            launch_num_speakers = (
                opts.diarization_num_speakers
                if opts.diarization_mode == "manual"
                else None
            )
            try:
                from services.providers.registry import get_provider_registry
                _diar_provider = get_provider_registry(db).get_diarization(session.organization_id)
                diarize_task = asyncio.create_task(_diar_provider.diarize(
                    str(audio_path),
                    num_speakers=launch_num_speakers,
                    clustering_threshold=opts.diarization_clustering_threshold,
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "upload %s: could not start concurrent diarization, "
                    "will run it synchronously after STT: %s",
                    upload_id, exc,
                )
                diarize_task = None

        await _set_job_state(db, job, stage="transcribing", progress_pct=5, session_id=session.id)
        try:
            transcription_result = await _transcribe_audio(
                audio_path,
                session.organization_id,
                db,
                provider_override=opts.stt_provider,
                model_override=opts.stt_model,
                language=opts.language or "en",
            )
        except BaseException:
            # STT failed — cancel the concurrent diarize task so it doesn't
            # outlive the request as an orphaned pending task.
            if diarize_task is not None:
                diarize_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await diarize_task
            raise
        try:
            await _persist_transcription(db, session, transcription_result)
        except BaseException:
            if diarize_task is not None:
                if not diarize_task.done():
                    diarize_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await diarize_task
            raise
        await _set_job_state(db, job, progress_pct=100, last_completed_stage="transcribing")

        # Diarization. Honor explicit off / manual count overrides; auto is
        # the default when the option is missing.
        if not diarization_off:
            await _set_job_state(db, job, stage="diarizing", progress_pct=10)
            num_speakers = (
                opts.diarization_num_speakers
                if opts.diarization_mode == "manual"
                else None
            )
            try:
                # 1) Await the diarization launched concurrently with STT
                # above; if that launch never started, run it now (fallback).
                if diarize_task is not None:
                    diar_segments = await diarize_task
                else:
                    from services.providers.registry import get_provider_registry
                    diar_provider = get_provider_registry(db).get_diarization(session.organization_id)
                    diar_segments = await diar_provider.diarize(
                        str(audio_path),
                        num_speakers=num_speakers,
                        clustering_threshold=opts.diarization_clustering_threshold,
                    )
                # 2) Merge labels onto the transcription segments + persist
                if diar_segments:
                    _assign_speakers_from_diarization(session, diar_segments)
                    db.commit()
                # 3) Optionally enrich with library matches (voice fingerprinting)
                fingerprint_enabled = (
                    True if opts.voice_fingerprint is None
                    else bool(opts.voice_fingerprint)
                )
                if fingerprint_enabled:
                    from services.speaker_service import identify_speakers
                    await asyncio.to_thread(identify_speakers, session, db)
                # v3.34.0 label parity: normalize residual raw diarizer codes
                # ("SPEAKER_00" -> "Speaker N") + resync the transcriptions
                # rows the UI renders — the upload pipeline skipped this, so
                # uploads showed raw codes in the transcript while the
                # summary said "Speaker 1" (audit finding #2).
                from services.speaker_labels import normalize_session_speaker_labels
                await asyncio.to_thread(normalize_session_speaker_labels, session, db)
                _clear_stage_needs_retry(session, "diarization")
                flag_modified(session, "processing_metadata")
                db.commit()
            except Exception as exc:
                logger.warning("Speaker diarization/identification failed for upload %s: %s", upload_id, exc)
                # Complete with the transcript, but flag the degraded stage so
                # the meeting doesn't silently finish with generic/no speakers
                # while the UI reports success — watchdog/manual reprocess retries.
                _flag_stage_needs_retry(session, "diarization", exc)
                flag_modified(session, "processing_metadata")
                db.commit()
            await _set_job_state(db, job, progress_pct=100, last_completed_stage="diarizing")

        await _set_job_state(db, job, stage="summarizing", progress_pct=10)
        # Best-effort summary: a summary-LLM failure (timeout, model unavailable,
        # ...) must NOT discard a successful transcript + diarization. On failure
        # complete the upload and flag the session needs_summary so it's retried
        # automatically (watchdog -> finalize_session_job) or via a manual
        # reprocess. Mirrors the always-on finalize path + the insights/index
        # stages below.
        try:
            await _summarize_session(
                db,
                session,
                template=opts.summary_template or "standard",
                llm_model=opts.llm_model,
                llm_thinking=opts.llm_thinking,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Summary failed for upload %s (session %s) — completing with "
                "transcript+diarization; flagged needs_summary for retry.",
                upload_id, session.id,
            )
            _flag_summary_needs_retry(session, exc)
            flag_modified(session, "processing_metadata")
            db.commit()
            # Kick the same retry job the watchdog uses so the summary fills in
            # automatically once the LLM recovers; best-effort, with the
            # needs_summary flag (watchdog / manual reprocess) as the fallback.
            try:
                from services.job_runner import get_arq_pool
                pool = await get_arq_pool()
                if pool is not None:
                    job_obj = await pool.enqueue_job(
                        "finalize_session_job",
                        session.id,
                        user_id=session.user_id,
                        org_id=session.organization_id,
                        _queue_name=os.getenv("ARQ_INTERACTIVE_QUEUE", "meeting-ops:interactive"),
                    )
                    if job_obj is not None:
                        session.processing_job_id = job_obj.job_id
                        db.commit()
            except Exception as enq_exc:  # noqa: BLE001
                logger.warning(
                    "Could not enqueue summary retry for session %s: %s — "
                    "needs_summary flag stands (watchdog / manual reprocess).",
                    session.id, enq_exc,
                )

        # Pre-warm the AI Insights side-panel so the first view of this
        # session doesn't trigger an LLM call. Without this the
        # /insights endpoint regenerates on first access (and on every
        # access if the cached payload fails validation), which Aaron
        # was seeing as the GPU kicking off on view. Best-effort: if
        # the insights call fails, the upload still completes; the
        # endpoint will lazily regenerate on first view.
        try:
            await _set_job_state(db, job, stage="summarizing", progress_pct=80)
            from api.ai_insights import _generate_ai_insights
            transcriptions = (
                db.query(Transcription)
                .filter(Transcription.session_id == session.id)
                .order_by(Transcription.start_time.asc())
                .all()
            )
            full_text = (session.transcript_simple or session.transcript or "").strip()
            insights = await _generate_ai_insights(
                full_text,
                transcriptions,
                session,
                db,
                session.organization_id,
            )
            session.ai_insights = insights.model_dump()
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pre-warm of ai_insights failed for session %s: %s", session.id, exc)
        await _set_job_state(db, job, progress_pct=100, last_completed_stage="summarizing")

        # Semantic index (Qdrant) for cross-meeting search + RAG. MUST mirror
        # recording.py reprocess "Stage 5.9": this interactive-upload pipeline
        # previously produced a transcript + summary that NEVER reached Qdrant,
        # so uploaded meetings were silently invisible to semantic search + RAG
        # chat (same silent-failure class as the always-on finalize path that
        # left sessions 496/499/502 at 0 points). Index AFTER identify_speakers
        # so diarized labels carry real names, and build the indexed transcript
        # FROM the diarized segments so the per-chunk ``speakers`` payload (and
        # therefore speaker-aware RAG) is populated. Best-effort + off-thread;
        # a failure never blocks the upload.
        try:
            await _set_job_state(db, job, stage="embedding", progress_pct=20)
            from services.semantic_search_service import semantic_search

            _segs = (
                session.transcript_diarized.get("segments")
                if isinstance(session.transcript_diarized, dict)
                else None
            )
            if _segs:
                _index_transcript = "\n".join(
                    f"{(seg.get('speaker') or 'Speaker')}: {(seg.get('text') or '').strip()}"
                    for seg in _segs
                    if (seg.get("text") or "").strip()
                )
            else:
                _index_transcript = session.transcript_simple or session.transcript or ""

            _index_summary = session.summary or ""
            if not _index_summary and isinstance(session.final_summary, dict):
                _index_summary = (
                    session.final_summary.get("executive")
                    or session.final_summary.get("summary")
                    or ""
                )

            await asyncio.to_thread(
                semantic_search.index_session,
                session_id=session.session_id or str(session.id),
                title=session.title or session.name or "",
                transcript=_index_transcript,
                summary=_index_summary,
                created_at=session.created_at.isoformat() if session.created_at else "",
                organization_id=session.organization_id,
            )
            await _set_job_state(db, job, progress_pct=100, last_completed_stage="embedding")
            logger.info(
                "Upload: semantic index updated for session=%s (%d diarized segs)",
                session.id,
                len(_segs) if _segs else 0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Upload semantic index failed (non-fatal) session=%s: %s",
                session.id,
                exc,
            )
            # Flag so the meeting isn't silently absent from cross-meeting
            # search/RAG while the UI reports a clean completion; a reprocess /
            # backfill re-indexes it.
            _flag_stage_needs_retry(session, "index", exc)
            flag_modified(session, "processing_metadata")
            db.commit()

        session.status = "completed"
        session.updated_at = datetime.now(timezone.utc)
        job.stage = "done"
        job.progress_pct = 100
        job.last_completed_stage = "done"
        job.job_completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        db.commit()

        # Participant stamping (parity with the always-on/reprocess finalize
        # path): the recorder, then any confirmed speaker->contact matches.
        # Fresh sessions + re-query so the just-committed status isn't lost.
        # Best-effort — a stamp failure never fails the upload.
        try:
            from api.recording import _autostamp_recorder_participant
            await _autostamp_recorder_participant(session.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("upload recorder stamp failed session=%s: %s", session.id, exc)
        try:
            await asyncio.to_thread(_stamp_speaker_contacts_fresh, session.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("upload speaker stamp failed session=%s: %s", session.id, exc)

        await ws_manager.broadcast(upload_id, _status_payload(job))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        db.rollback()
        job = db.query(UploadJob).filter(UploadJob.upload_id == uuid.UUID(upload_id)).first()
        if job:
            job.stage = "failed"
            job.error_message = str(exc)[:2000]
            job.job_completed_at = datetime.now(timezone.utc)
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            await ws_manager.broadcast(upload_id, _status_payload(job))
        raise
    finally:
        db.close()


async def _extract_audio(source_path: Path, audio_path: Path, upload_id: str, db: Session, job: UploadJob) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(source_path), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(audio_path),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    stderr = []
    while True:
        line = await proc.stderr.readline() if proc.stderr else b""
        if not line:
            break
        text = line.decode(errors="ignore")
        stderr.append(text)
        match = re.search(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if match:
            seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
            pct = min(95, int(seconds))
            await _set_job_state(db, job, progress_pct=pct)
    code = await proc.wait()
    if code != 0 or not audio_path.exists():
        raise RuntimeError("Audio extraction failed: " + "".join(stderr)[-500:])


async def _create_upload_session(db: Session, job: UploadJob, audio_path: Path) -> RecordingSession:
    # Run the shared filename parser. Pattern 1 (notes__/downloads__ Mac
    # Notes + Voice Memos export) gives us title + meeting_date +
    # meeting_time + source in one shot; patterns 2-5 cover generic ISO /
    # US-date filenames. parsed.title is always populated (best-effort
    # stem cleanup when nothing matches) so the session never lands
    # without a title.
    from utils.filename_parser import parse_filename
    parsed = parse_filename(job.filename or "")

    fallback_title = _title_from_filename(job.filename) or "Uploaded Meeting"
    title = (parsed.title or fallback_title)[:200]

    # started_at / created_at stay the actual server ingest time so
    # processing analytics aren't poisoned by historical backfill. The
    # new meeting_date / meeting_time columns are the user-editable
    # "when the meeting actually happened" view.
    server_now = datetime.now(timezone.utc)

    # Assemble the when/where/who provenance ledger and pick the best
    # "recorded at" signal (audio container creation_time > filename date >
    # file mtime > upload time). meeting_date/time follow that estimate (the
    # user-facing "when it happened"); created_at/started_at do NOT — they stay
    # the server INGEST time (server_now). The session watchdog (stuck-processing
    # age) and retention/media-eviction all measure age from created_at, so
    # backdating it to an imported recording's original date would make a fresh
    # import look "stuck" and immediately retention-/eviction-eligible (silent
    # data loss). The historical timestamp is preserved in meeting_date /
    # meeting_time + the provenance ledger (when.recorded_at).
    from services.meeting_provenance import (
        build_meeting_provenance,
        recorded_date_time,
    )
    provenance = build_meeting_provenance(
        audio_path=audio_path,
        original_filename=job.filename,
        filename_date=parsed.meeting_date,
        filename_time=parsed.meeting_time,
        uploaded_at=server_now,
        source_type="upload",
        client_modified_at=job.client_modified_at,
    )
    recorded = provenance["when"]["recorded_at"]
    meeting_date = parsed.meeting_date
    meeting_time = parsed.meeting_time
    if meeting_date is None:
        rec_d, rec_t = recorded_date_time(recorded)
        meeting_date = rec_d
        meeting_time = meeting_time or rec_t

    session = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        description=f"Uploaded from {job.filename}",
        meeting_type="upload",
        created_at=server_now,
        started_at=server_now,
        meeting_date=meeting_date,
        meeting_time=meeting_time,
        status="processing",
        user_id=job.user_id,
        organization_id=job.organization_id,
        audio_file=str(audio_path),
        source_type="upload",
        processing_metadata={
            "upload_id": str(job.upload_id),
            # Same key the bulk-import path writes, so one de-dupe lookup
            # covers sessions created by either path.
            "audio_sha256": job.audio_sha256,
            "original_filename": job.filename,
            "content_type": job.content_type,
            "filename_parse": {
                "confidence": parsed.confidence,
                "source": parsed.source,
                "matched_title": parsed.title,
            },
            "meeting_provenance": provenance,
        },
    )
    db.add(session)
    db.flush()
    job.session_id = session.id
    db.commit()
    db.refresh(session)
    _persist_upload_audio_to_garage(db, session, audio_path)
    return session


async def _attach_to_existing_session(db: Session, job: UploadJob, audio_path: Path) -> RecordingSession:
    """Bind the uploaded audio onto a pre-existing RecordingSession.

    The session row was selected at /uploads/start and its id stored on
    job.session_id. We update the audio path, mark it processing, and let the
    pipeline transcribe / diarize / summarize / embed exactly as it would for
    a brand-new upload — so attach gets the same provider-resolved STT and
    LLM as any other upload, instead of going through the legacy NPU-first
    path the old api/audio_upload.py used.
    """
    if not job.session_id:
        raise RuntimeError("attach upload has no target_session_id on the job row.")
    session = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.id == job.session_id,
            RecordingSession.organization_id == job.organization_id,
        )
        .first()
    )
    if not session:
        raise RuntimeError(f"target session {job.session_id} not found in org {job.organization_id}.")

    session.audio_file = str(audio_path)
    session.status = "processing"
    session.updated_at = datetime.now(timezone.utc)
    metadata = dict(session.processing_metadata or {})
    metadata.update({
        "attached_upload_id": str(job.upload_id),
        "attached_filename": job.filename,
        "attached_content_type": job.content_type,
        "attached_at": datetime.now(timezone.utc).isoformat(),
    })
    session.processing_metadata = metadata
    db.commit()
    db.refresh(session)
    _persist_upload_audio_to_garage(db, session, audio_path)
    return session


def _persist_upload_audio_to_garage(db: Session, session: RecordingSession, audio_path: Path) -> None:
    """Best-effort durable copy of the uploaded audio to Garage. Local file
    stays the working copy; never blocks the upload pipeline."""
    try:
        from services.session_media import persist_session_audio
        persist_session_audio(db, session, local_path=str(audio_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Garage persist failed for upload session=%s: %s", session.id, exc)


async def _resolve_session_for_job(db: Session, job: UploadJob, audio_path: Path) -> RecordingSession:
    """Branch on job.action: attach reuses the target session, transcribe creates a new one."""
    if job.action == "attach":
        return await _attach_to_existing_session(db, job, audio_path)
    return await _create_upload_session(db, job, audio_path)


async def _transcribe_audio(
    audio_path: Path,
    org_id: int,
    db: Session,
    *,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
    language: str = "en",
) -> dict[str, Any]:
    """Transcribe via the configured STT provider (Whisper or Parakeet).

    Diarization runs separately after this step. The provider is resolved
    through ProviderRegistry; per-upload overrides win over the org's
    saved Provider Settings so a single recording can route through
    Parakeet while the org default stays Whisper (or vice versa).
    """
    from services.providers.registry import get_provider_registry
    from services.transcription_options import normalized_provider_name, TranscriptionOptions
    registry = get_provider_registry(db)
    norm_provider = normalized_provider_name(
        TranscriptionOptions(stt_provider=provider_override) if provider_override else TranscriptionOptions()
    )
    stt = registry.get_stt(
        org_id,
        provider_override=norm_provider,
        model_override=model_override,
    )
    result = await asyncio.wait_for(stt.transcribe(str(audio_path), language=language), timeout=1800)
    if not result or not result.get("segments"):
        # Surface the provider's real reason (STT "audio too long" / 500 /
        # timeout, carried on result["error"]) instead of a generic message,
        # so the job's error_message tells the user what actually happened.
        raise RuntimeError((result or {}).get("error") or "Transcription produced no segments.")
    # _persist_transcription expects audio_duration; the providers return
    # `duration` directly, so mirror it for back-compat.
    result.setdefault("audio_duration", result.get("duration", 0.0))
    # Default-default reflects the current cluster STT (Parakeet 1.1B on
    # midboy2). Both LocalParakeetProvider and LocalWhisperProvider already
    # set "model" on success; this setdefault only matters if a provider
    # returns an empty dict (timeout/error path).
    result.setdefault("model", "parakeet-tdt-1.1b")
    return result


def _embedding_for_utterance(
    utt_start: float,
    utt_end: float,
    utt_label: Optional[str],
    diar_sorted: list[dict],
) -> Optional[list[float]]:
    """Return the per-segment embedding that best represents this utterance.

    The diarizer attaches a 256-d embedding to each turn (`return_embeddings=true`
    in the speaker-svc call). When we collapse word-aligned turns into
    utterances we lose the 1:1 segment->embedding mapping; restore it by
    picking the diarization turn with the same speaker label that overlaps
    the utterance time-range the most. Fall back to longest matching-label
    turn anywhere in the recording if no time overlap (rare — only happens
    when the speaker dict points at a single tiny turn).
    """
    if not utt_label or not diar_sorted:
        return None
    best_emb: Optional[list[float]] = None
    best_overlap = 0.0
    best_label_dur = 0.0
    best_label_emb: Optional[list[float]] = None
    for d in diar_sorted:
        if d.get("speaker") != utt_label:
            continue
        d_start = float(d.get("start") or 0)
        d_end = float(d.get("end") or 0)
        emb = d.get("embedding")
        if not emb:
            continue
        overlap = max(0.0, min(utt_end, d_end) - max(utt_start, d_start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_emb = emb
        # Track longest same-label turn as a fallback if there's no overlap
        # at all (shouldn't happen for well-aligned data but keeps the
        # function robust for edge cases like single-word utterances).
        dur = d_end - d_start
        if dur > best_label_dur:
            best_label_dur = dur
            best_label_emb = emb
    return best_emb if best_emb is not None else best_label_emb


def _build_segments_from_words(
    words: list[dict],
    diar_segments: list[dict],
) -> list[dict]:
    """Group word-level timestamps into per-speaker utterances.

    For each word: find the pyannote turn whose [start, end] contains the
    word's midpoint, and assign that turn's speaker label. Then walk the
    labeled words left-to-right, starting a new utterance whenever the
    speaker label changes (or there's a gap > 0.8s of silence). Each
    utterance carries the joined text + start/end of its first/last word.

    Embeddings: when the diarization turns carry per-segment embeddings
    (speaker-svc /diarize?return_embeddings=true), we attach the embedding
    of the diarization turn that maximally overlaps the utterance. This
    preserves the data identify_speakers() needs to match enrolled
    voices — without it the by-label embedding lookup falls through with
    reason=no_embedding and no auto-links get created.
    """
    if not words or not diar_segments:
        return []

    diar_sorted = sorted(diar_segments, key=lambda s: float(s.get("start") or 0))
    starts = [float(d.get("start") or 0) for d in diar_sorted]
    ends = [float(d.get("end") or 0) for d in diar_sorted]
    labels = [d.get("speaker") for d in diar_sorted]

    def _speaker_for(mid: float) -> Optional[str]:
        # Linear scan is fine — diar_sorted is rarely >2000 turns.
        for s, e, lab in zip(starts, ends, labels):
            if s <= mid <= e:
                return lab
        # Fallback: nearest turn boundary.
        best_label = None
        best_dist = float("inf")
        for s, e, lab in zip(starts, ends, labels):
            d = min(abs(mid - s), abs(mid - e))
            if d < best_dist:
                best_dist = d
                best_label = lab
        return best_label

    raw_utterances: list[dict] = []
    cur_speaker: Optional[str] = None
    cur_words: list[dict] = []
    cur_start = 0.0
    last_end = 0.0
    SILENCE_GAP = 0.8

    for w in words:
        try:
            w_start = float(w.get("start") or 0)
            w_end = float(w.get("end") or 0)
        except (TypeError, ValueError):
            continue
        word_text = (w.get("word") or w.get("text") or "").strip()
        if not word_text:
            continue
        mid = (w_start + w_end) / 2
        speaker = _speaker_for(mid)

        new_speaker = speaker != cur_speaker
        gap_break = (w_start - last_end) > SILENCE_GAP and cur_words
        if new_speaker or gap_break:
            if cur_words:
                raw_utterances.append({
                    "start": cur_start,
                    "end": last_end,
                    "speaker": cur_speaker,
                    "text": " ".join(x.get("word", "") for x in cur_words).strip(),
                })
            cur_words = []
            cur_speaker = speaker
            cur_start = w_start
        cur_words.append(w)
        last_end = w_end

    if cur_words:
        raw_utterances.append({
            "start": cur_start,
            "end": last_end,
            "speaker": cur_speaker,
            "text": " ".join(x.get("word", "") for x in cur_words).strip(),
        })

    # Attach per-utterance embeddings (picked from the matching diar turn
    # with the most overlap). Skip if no diar turn carried embeddings.
    has_any_emb = any(d.get("embedding") for d in diar_sorted)
    if not has_any_emb:
        return raw_utterances
    for u in raw_utterances:
        emb = _embedding_for_utterance(
            float(u.get("start") or 0),
            float(u.get("end") or 0),
            u.get("speaker"),
            diar_sorted,
        )
        if emb is not None:
            u["embedding"] = emb
    return raw_utterances


def _assign_speakers_from_diarization(session: RecordingSession, diar_segments: list[dict]) -> None:
    """Merge diarization output onto the transcription segments.

    Two paths:

    1. Word-level alignment (preferred — NeMo 2.4+ Parakeet returns word
       timestamps). Walks each word, picks the pyannote turn containing
       its midpoint, and groups adjacent words by speaker into proper
       per-speaker utterances. Produces a transcript that looks like
       "Speaker A: ...\nSpeaker B: ...\nSpeaker A: ..." with accurate
       turn boundaries.

    2. Segment-overlap fallback. If no word timestamps are available,
       walk the existing transcript_diarized.segments and label each
       with the diarization-segment that overlaps it most. If the
       transcription is under-segmented relative to diarization, replace
       the segment list with pyannote's turns so SpeakerTagger still
       sees all the labels (text will be empty on those segments but
       transcript_simple preserves the full text).
    """
    from sqlalchemy.orm.attributes import flag_modified

    if not diar_segments:
        return
    transcript = session.transcript_diarized
    if not isinstance(transcript, dict):
        transcript = {}

    # --- Path 1: word-level alignment (preferred) ---------------------------
    words = []
    pm = session.processing_metadata or {}
    if isinstance(pm, dict):
        words = pm.get("word_timestamps") or []

    if words and isinstance(words, list):
        utterances = _build_segments_from_words(words, diar_segments)
        if utterances:
            speakers_ordered: list[str] = []
            seen: set[str] = set()
            for u in utterances:
                spk = u.get("speaker")
                if spk and spk not in seen:
                    seen.add(spk)
                    speakers_ordered.append(spk)
            transcript["segments"] = utterances
            transcript["speakers"] = speakers_ordered
            transcript["diarization_backend"] = (
                diar_segments[0].get("backend") if diar_segments else None
            )
            transcript["aligned_by"] = "word_timestamps"
            session.transcript_diarized = transcript
            flag_modified(session, "transcript_diarized")
            # Also refresh the Transcription rows with the new per-utterance
            # segments so list/detail views render speaker labels.
            from sqlalchemy.orm.session import Session as _SAS
            db = getattr(session, "_sa_instance_state", None) and session._sa_instance_state.session  # type: ignore[union-attr]
            if db is not None:
                db.query(Transcription).filter(Transcription.session_id == session.id).delete()
                for u in utterances:
                    if not u.get("text"):
                        continue
                    db.add(Transcription(
                        session_id=session.id,
                        text=u["text"],
                        speaker=u.get("speaker"),
                        start_time=float(u.get("start") or 0),
                        end_time=float(u.get("end") or 0),
                        confidence=0.95,
                    ))
            return

    # --- Path 2: under-segmentation fallback --------------------------------
    segments = transcript.get("segments") or []
    # If the stored segments are themselves a PREVIOUS diarizer's turns
    # (used_pyannote_segmentation — text is empty, nothing to preserve),
    # replace them with the fresh turns instead of relabeling in place.
    # Relabel-in-place kept stale raw_labels and let non-overlapping turns
    # keep their old speaker, so a re-detect with num_speakers=2 over an
    # earlier 4-cluster result still surfaced 4 speakers.
    if transcript.get("used_pyannote_segmentation") or len(segments) <= max(1, len(diar_segments) // 10):
        new_segments = []
        for d in diar_segments:
            seg_dict = {
                "start": float(d.get("start") or 0),
                "end": float(d.get("end") or 0),
                "speaker": d.get("speaker"),
                "text": "",
            }
            # Preserve per-segment embeddings so identify_speakers can
            # match enrolled voices. The diarizer (speaker-svc with
            # return_embeddings=true) attaches a 256-d float list per turn.
            if d.get("embedding"):
                seg_dict["embedding"] = d["embedding"]
            new_segments.append(seg_dict)
        speakers_ordered: list[str] = []
        seen: set[str] = set()
        for d in new_segments:
            spk = d.get("speaker")
            if spk and spk not in seen:
                seen.add(spk)
                speakers_ordered.append(spk)
        transcript["segments"] = new_segments
        transcript["speakers"] = speakers_ordered
        transcript["diarization_backend"] = (
            diar_segments[0].get("backend") if diar_segments else None
        )
        transcript["used_pyannote_segmentation"] = True
        session.transcript_diarized = transcript
        flag_modified(session, "transcript_diarized")

        # Also relabel the Transcription rows so the frontend (which
        # prefers transcription_segments) shows speakers. Each row gets
        # the speaker that overlaps it the most. Without this, sessions
        # processed before NeMo 2.4 (no word timestamps) render every
        # row as the generic 'Speaker' fallback even though pyannote
        # found the right speakers.
        diar_sorted = sorted(diar_segments, key=lambda s: float(s.get("start") or 0))

        def _dominant_speaker(row_start: float, row_end: float) -> Optional[str]:
            # Tally total overlap-seconds per speaker label within
            # [row_start, row_end], then pick the heaviest. Linear scan
            # is fine — diar_segments is usually <1000.
            tally: dict[str, float] = {}
            for d in diar_sorted:
                d_start = float(d.get("start") or 0)
                d_end = float(d.get("end") or 0)
                if d_end < row_start:
                    continue
                if d_start > row_end:
                    break
                ov = max(0.0, min(row_end, d_end) - max(row_start, d_start))
                if ov <= 0:
                    continue
                label = d.get("speaker") or ""
                if not label:
                    continue
                tally[label] = tally.get(label, 0.0) + ov
            if not tally:
                return None
            return max(tally.items(), key=lambda kv: kv[1])[0]

        db = getattr(session, "_sa_instance_state", None) and session._sa_instance_state.session
        if db is not None:
            rows = db.query(Transcription).filter(Transcription.session_id == session.id).all()
            for row in rows:
                label = _dominant_speaker(
                    float(row.start_time or 0),
                    float(row.end_time or 0),
                )
                if label:
                    row.speaker = label
        return

    # Sort diarization segments once; use a small linear scan per segment
    # since both lists are usually <500 items.
    diar_sorted = sorted(diar_segments, key=lambda s: float(s.get("start") or 0))

    def _label_and_embedding_for(
        seg_start: float, seg_end: float
    ) -> tuple[Optional[str], Optional[list[float]]]:
        best_label: Optional[str] = None
        best_overlap = 0.0
        best_embedding: Optional[list[float]] = None
        for d in diar_sorted:
            d_start = float(d.get("start") or 0)
            d_end = float(d.get("end") or 0)
            if d_end < seg_start:
                continue
            if d_start > seg_end:
                break
            overlap = max(0.0, min(seg_end, d_end) - max(seg_start, d_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_label = d.get("speaker")
                best_embedding = d.get("embedding")
        return best_label, best_embedding

    def _label_for(seg_start: float, seg_end: float) -> Optional[str]:
        return _label_and_embedding_for(seg_start, seg_end)[0]

    for seg in segments:
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or 0)
        label, embedding = _label_and_embedding_for(start, end)
        if label:
            seg["speaker"] = label
            # The old raw_label belongs to the PREVIOUS diarization run —
            # keeping it makes identify/re-link (and the rediarize unique
            # count) resurrect speakers the new run no longer has.
            seg.pop("raw_label", None)
            # Preserve per-segment embeddings so identify_speakers can
            # match enrolled voices. Without this the by-label embedding
            # lookup falls through with reason=no_embedding and no
            # auto-links get created.
            if embedding:
                seg["embedding"] = embedding

    transcript["segments"] = segments
    # Rebuild the roster from the final labels — the pre-existing list can
    # still name speakers from an older run.
    speakers_ordered = []
    _seen: set[str] = set()
    for seg in segments:
        spk = seg.get("speaker")
        if spk and spk not in _seen:
            _seen.add(spk)
            speakers_ordered.append(spk)
    transcript["speakers"] = speakers_ordered
    transcript["diarization_backend"] = (
        diar_sorted[0].get("backend") if diar_sorted else None
    )
    session.transcript_diarized = transcript
    flag_modified(session, "transcript_diarized")

    # Also rewrite the per-segment Transcription rows so list/detail views
    # render speaker labels even when they don't go through transcript_diarized.
    from sqlalchemy.orm.session import Session as _SAS  # local import to avoid module cycle
    db: Optional[_SAS] = getattr(session, "_sa_instance_state", None) and session._sa_instance_state.session  # type: ignore[union-attr]
    if db is None:
        return
    rows = db.query(Transcription).filter(Transcription.session_id == session.id).all()
    for row in rows:
        label = _label_for(float(row.start_time or 0), float(row.end_time or 0))
        if label:
            row.speaker = label


async def _persist_transcription(db: Session, session: RecordingSession, result: dict[str, Any]) -> None:
    segments = result.get("segments", [])
    session.transcript_diarized = result
    session.transcript_simple = " ".join(s.get("text", "") for s in segments if s.get("text")).strip()
    session.transcript = json.dumps(result)
    audio_duration = result.get("audio_duration") or max((float(s.get("end", 0)) for s in segments), default=0)
    session.duration = float(audio_duration or 0)
    db.query(Transcription).filter(Transcription.session_id == session.id).delete()
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        db.add(Transcription(
            session_id=session.id,
            text=text,
            speaker=segment.get("speaker"),
            start_time=float(segment.get("start") or 0),
            end_time=float(segment.get("end") or 0),
            confidence=float(segment.get("confidence") or 0.95),
        ))
    # JSON column: copy then flag_modified so SQLAlchemy actually emits
    # the UPDATE. Without this, mutating the same dict reference and the
    # following db.refresh() throws the new keys away — meaning
    # word_timestamps never reaches the DB and Re-detect can't run Path 1.
    metadata = dict(session.processing_metadata or {})
    metadata.update({
        "word_count": len((session.transcript_simple or "").split()),
        "transcription_model": result.get("model", "whisper"),
        "transcription_language": result.get("language", "en"),
        "npu_accelerated": result.get("npu_accelerated", False),
        "processing_started_from_upload": True,
    })
    word_timestamps = result.get("words") or []
    if word_timestamps:
        metadata["word_timestamps"] = word_timestamps
        metadata["has_word_timestamps"] = True
    rtf = result.get("rtf")
    if rtf is not None:
        metadata["transcription_rtf"] = rtf
    session.processing_metadata = metadata
    flag_modified(session, "processing_metadata")
    db.commit()
    db.refresh(session)


_SUMMARY_TEMPLATE_GUIDANCE: dict[str, str] = {
    "standard": (
        "Produce a balanced meeting summary covering main topics, key "
        "decisions, action items, and follow-ups. Keep tone neutral and "
        "useful for someone who missed the meeting."
    ),
    "executive": (
        "Produce an executive-style summary: lead with strategic implications "
        "and decisions, then action items with owners, then a one-line "
        "recap. Skip routine status updates."
    ),
    "technical": (
        "Produce a technical summary: architecture/design decisions, "
        "implementation details, technical debt mentioned, dependencies and "
        "blockers, then action items. Keep code/system terms verbatim."
    ),
}


# When MEETING_OPS_SUMMARIZER_URL is set, the upload pipeline's summary step
# routes directly there instead of through the per-org ProviderRegistry/
# LiteLLM gateway. Keeps the hop count low (fewer things to debug) and lets
# us reuse the Qwen3.6-35B-A3B-Vision instance already loaded for David's
# listing-ops flow on midboy1:8088. v1 only — long-term this should fold
# back into ProviderRegistry for unified usage metering and per-org auth.
# System prompt + style template ported from the Meeting Minutes Mac app
# (scripts/summarize.py qwen3.6-moe variant). Same wording so the two
# stacks produce comparable output. The fenced-JSON tail at the bottom
# of the user prompt is meeting-ops-specific — our DB stores structured
# fields (bullets, actions[], decisions[], title) so we ask the model to
# emit both the human-readable markdown AND a JSON payload at the end.
MEETING_OPS_SUMMARIZER_SYSTEM_PROMPT = """You are an expert executive assistant specializing in meeting analysis and summarization. Your task is to produce comprehensive, well-structured meeting notes from transcripts.

Critical rules you must follow:
1. Only include information explicitly stated in the transcript. Never fabricate details.
2. If an action item owner or deadline is not mentioned, write "Not specified."
3. Use direct quotes only when the speaker's exact words appear in the transcript.
4. Attribute all statements and decisions to the specific speaker who made them.
5. Format your output with clear markdown structure using headers, lists, and tables.
6. Be thorough but concise - include all substantive points without padding."""


# The user-prompt scaffold. Speaker context (if any) plus the transcript
# get spliced in by _summarize_session().
MEETING_OPS_SUMMARIZER_STYLE_PROMPT = """Analyze the following meeting transcript and produce comprehensive, structured meeting notes.

{speaker_context}

Output the following sections in order. Include ALL substantive information from the transcript.

## Executive Summary
Write 2-3 sentences capturing the meeting's main purpose, key outcomes, and overall tone.

## Key Discussion Points
For each major topic discussed, create a numbered section:
1. **[Topic Title]**
   - [Key point] ([Speaker Name] raised this)
   - [Supporting detail or counterpoint] ([Speaker Name])
   Include all perspectives shared, areas of agreement, and disagreements.

## Action Items
Create a markdown table with these columns:
| Owner | Action | Priority | Deadline |
For each action item explicitly stated in the transcript. If owner/deadline not stated, write "Not specified."

## Key Decisions
List each decision made during the meeting:
- **[Decision]** - Made by [who], rationale: [why, if discussed]

## Key Quotes
Select up to 5 notable quotes that capture important positions, commitments, or insights:
> "[Exact words from transcript]" - [Speaker Name]
> Context: [One sentence explaining why this quote matters]

## Next Steps
- [Next step with responsible person if mentioned]

## Open Questions
- [Any unresolved items that need follow-up]

IMPORTANT: If a section has no genuine content in the transcript, write "None identified." under that heading — never invent, pad, or infer items to fill a section. Quote only words that actually appear in the transcript.

---
TRANSCRIPT:
{transcript}

---
After the markdown sections above, emit a single fenced JSON block with the same content split into structured keys. This is required for downstream tooling. The fenced block looks like:

```json
{{"executive": "...",
  "bullets": ["topic 1: point", "topic 2: point", ...],
  "actions": [{{"action": "...", "owner": "...", "priority": "high|medium|low", "deadline": "..."}}],
  "decisions": ["decision (decided by Speaker)", ...],
  "questions": ["..."],
  "quotes": [{{"text": "...", "speaker": "...", "context": "..."}}],
  "next_steps": ["..."],
  "minutes": "the full markdown output above as a single string",
  "title": "short descriptive title for this meeting"}}
```
"""

# Lighter prompt for short recordings and single-speaker memos (v3.35.0,
# audit #6). The full 7-section format forces fabricated-looking output on a
# 60-second voice memo — quotes nobody needs, "implied" actions, multi-party
# framing ("areas of agreement") that reads absurd for one voice. Same fenced
# JSON contract so _parse_summarizer_json is unchanged; empty keys stay [].
MEETING_OPS_MEMO_PROMPT = """Summarize the following short recording (a brief meeting or a single-speaker memo).

{speaker_context}

Output exactly these sections:

## Summary
2-4 sentences capturing what this recording is about and any outcome.

## Key Points
- The substantive points made, in order. Keep it tight.

## Action Items
Only actions EXPLICITLY stated. If none, write "None identified." — never invent or infer items.

---
TRANSCRIPT:
{transcript}

---
After the markdown above, emit a single fenced JSON block (required for downstream tooling).
"executive" MUST be the 2-4 sentence Summary prose from above — what the recording was
about and any outcome. NEVER put a person's name, a speaker label, or the title in
"executive"; it is a summary, not a name. Put the short title in "title".

```json
{{"executive": "the 2-4 sentence summary from the Summary section above",
  "bullets": ["point", ...],
  "actions": [{{"action": "...", "owner": "...", "priority": "medium", "deadline": "Not specified"}}],
  "decisions": [],
  "questions": [],
  "quotes": [],
  "next_steps": [],
  "minutes": "the full markdown output above as a single string",
  "title": "short descriptive title"}}
```
"""


def _direct_summarizer_provider():
    """Build a LiteLLMProvider pointing at the direct meeting-ops LLM
    endpoint, or return None when no env is configured (caller falls back
    to ProviderRegistry).

    Prefers MEETING_OPS_LLM_URL (the broader knob that ProviderRegistry
    also reads). Falls back to MEETING_OPS_SUMMARIZER_URL for backward
    compat with the earlier deploy that only had a summarizer-specific
    override.
    """
    base = os.getenv("MEETING_OPS_LLM_URL", "").strip() or os.getenv("MEETING_OPS_SUMMARIZER_URL", "").strip()
    model = os.getenv("MEETING_OPS_LLM_MODEL", "").strip() or os.getenv("MEETING_OPS_SUMMARIZER_MODEL", "").strip()
    if not base or not model:
        return None
    from services.providers.impl_llm import LiteLLMProvider
    # Fall back to the suite-standard gateway key (OPENAI_API_KEY / LITELLM_API_KEY)
    # when the MO-specific override vars are unset. The summarizer originally hit
    # a keyless local llama.cpp box directly, so an empty api_key was fine; once
    # the endpoint was repointed at the LiteLLM gateway (unicorn-litellm:4000),
    # which requires auth, an empty key made every summary/insight call 401
    # ("No api key passed in") and finalize_session_job silently failed. Every
    # container already injects OPENAI_API_KEY=${LITELLM_API_KEY}, so this makes
    # the summarizer honor the standard convention without any env/compose change.
    return LiteLLMProvider(
        api_key=(
            os.getenv("MEETING_OPS_LLM_API_KEY", "")
            or os.getenv("MEETING_OPS_SUMMARIZER_API_KEY", "")
            or os.getenv("OPENAI_API_KEY", "")
            or os.getenv("LITELLM_API_KEY", "")
        ),
        endpoint=base,
        model=model,
        thinking=False,  # Qwen 3.6 emits <think> by default; suppress.
    )



_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"(\{(?:[^{}]|\{[^{}]*\})*\})", re.DOTALL)
_SUMMARY_SECTION_RE = re.compile(
    r"##+\s*(?:Executive\s+)?Summary\s*\n+(.+?)(?=\n##+\s|\n```|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def _extract_summary_section(text: str) -> str:
    """Pull the markdown '## Summary' (or '## Executive Summary') body out of a
    summarizer response — the memo executive-summary fallback when the LLM left
    the JSON `executive` field empty or filled it with a bare name/title."""
    m = _SUMMARY_SECTION_RE.search(text or "")
    if not m:
        return ""
    # Drop leading list bullets / stray markdown so we keep clean prose.
    body = " ".join(line.strip().lstrip("-*").strip() for line in m.group(1).splitlines())
    return " ".join(body.split()).strip()


def _parse_summarizer_json(text: str, *, fallback_title: str) -> dict[str, Any]:
    """Pull the JSON object the summarizer was asked to emit.

    Qwen 3.6 emits prose then a ```json fenced block; older models return
    bare JSON. Try fenced first, then bare, then surface the raw text as
    the executive summary so the rest of the pipeline still gets
    something readable.
    """
    text = text or ""
    for pattern in (_FENCED_JSON_RE, _BARE_JSON_RE):
        for match in pattern.finditer(text):
            blob = match.group(1)
            try:
                data = json.loads(blob)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
    return {
        "executive": text.strip() or "Summary generation returned no content.",
        "bullets": [],
        "actions": [],
        "decisions": [],
        "title": fallback_title,
    }



def _markdown_from_response(text: str) -> str:
    """Strip the trailing fenced JSON block so we can store the human-
    readable markdown as final_summary.minutes. If there is no fence,
    returns the full text unchanged."""
    if not text:
        return ""
    import re
    m = re.search(r"```(?:json)?\s*\{", text)
    if m:
        return text[: m.start()].rstrip()
    return text.strip()


# ---------------------------------------------------------------------------
# Map-reduce summarization (v3.36.0) — full coverage of arbitrarily long
# meetings, behind MEETING_OPS_SUMMARY_MAPREDUCE (default OFF).
#
# The single-call path (above/below) caps the transcript at ~50K/60K tokens,
# so an 8-hour meeting (~96K tokens) is summarized only through its first
# ~4-5h and stamped `summary_truncated`. The back half is lost. When the
# toggle is ON and the attributed transcript exceeds the chunk size, we
# instead: split the diarized speaker-turns into chunk_tokens windows (never
# mid-utterance, with overlap for continuity), summarize each chunk into a
# small structured JSON (MAP), then synthesize all the chunk-JSONs into the
# final summary in the EXACT existing final_summary contract (REDUCE). Every
# LLM call is bounded to the chunk size, so the whole meeting is covered.
#
# The reduce step deliberately emits the SAME fenced markdown + JSON a
# single call produces, so its output flows through the identical
# parse/write block in _summarize_session — the contract cannot drift.
# Best-effort like the single-call summarizer: any failure (a map chunk or
# the reduce) logs a warning and falls back to the single-call (truncated)
# path; it never crashes the finalize pipeline.
# ---------------------------------------------------------------------------


def _mapreduce_enabled() -> bool:
    return os.getenv("MEETING_OPS_SUMMARY_MAPREDUCE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _estimate_tokens(text: str) -> int:
    """Rough token estimate. No tokenizer is bundled in this stack (even
    ai_chat.py budgets by raw chars), so we use the standard ~4 chars/token
    heuristic. Only used to decide chunk boundaries — approximate is fine."""
    return len(text or "") // 4


def _mapreduce_chunk_tokens() -> int:
    try:
        return max(2000, int(os.getenv("MEETING_OPS_SUMMARY_CHUNK_TOKENS", "28000")))
    except ValueError:
        return 28000


def _mapreduce_overlap_tokens() -> int:
    try:
        return max(0, int(os.getenv("MEETING_OPS_SUMMARY_CHUNK_OVERLAP_TOKENS", "1500")))
    except ValueError:
        return 1500


def _fmt_clock(seconds: float) -> str:
    """Seconds -> H:MM:SS (or M:SS) for the per-chunk time range labels."""
    try:
        total = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _chunk_segments(
    segments: list[dict[str, Any]],
    label_map: Optional[dict[str, str]],
    *,
    chunk_tokens: int,
    overlap_tokens: int,
) -> list[dict[str, Any]]:
    """Split diarized segments into ~chunk_tokens windows on speaker-turn
    boundaries (never mid-segment), carrying ~overlap_tokens of the previous
    window's trailing segments into the next for continuity.

    Each returned chunk is ``{"index", "text", "speakers", "start", "end"}``
    where ``text`` is a speaker-attributed transcript (same renderer the
    single-call path uses) and start/end are the chunk's rough time range.

    A single segment larger than chunk_tokens becomes its own (oversized)
    chunk rather than being split mid-utterance — the per-chunk LLM call can
    still summarize it; only the byte cap of one call is exceeded, by design,
    to keep speaker turns intact.
    """
    from services.speaker_labels import build_attributed_transcript

    # Keep only segments with real text; preserve order (callers pass the
    # diarized order, which build_attributed_transcript also relies on).
    usable = [s for s in segments if (s.get("text") or "").strip()]
    if not usable:
        return []

    def _seg_tokens(seg: dict[str, Any]) -> int:
        return _estimate_tokens(seg.get("text") or "")

    chunks: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []
    cur_tok = 0

    def _emit(segs: list[dict[str, Any]]) -> None:
        if not segs:
            return
        text, speakers = build_attributed_transcript(segs, label_map)
        if not text.strip():
            return
        starts = [float(s.get("start") or 0) for s in segs]
        ends = [float(s.get("end") or s.get("start") or 0) for s in segs]
        chunks.append({
            "index": len(chunks),
            "text": text,
            "speakers": speakers,
            "start": min(starts) if starts else 0.0,
            "end": max(ends) if ends else 0.0,
        })

    for seg in usable:
        tok = _seg_tokens(seg)
        # Adding this turn would overflow the window -> close the current
        # window first, then seed the next one with an overlap tail.
        if cur and cur_tok + tok > chunk_tokens:
            _emit(cur)
            # Carry trailing segments (~overlap_tokens) into the next window.
            tail: list[dict[str, Any]] = []
            tail_tok = 0
            for prev in reversed(cur):
                ptok = _seg_tokens(prev)
                if tail and tail_tok + ptok > overlap_tokens:
                    break
                tail.insert(0, prev)
                tail_tok += ptok
                if tail_tok >= overlap_tokens:
                    break
            cur = list(tail)
            cur_tok = tail_tok
        cur.append(seg)
        cur_tok += tok
        # A lone giant segment already exceeds the window on its own -> flush
        # it as its own chunk so it is never split mid-utterance.
        if cur_tok >= chunk_tokens and len(cur) == 1:
            _emit(cur)
            cur = []
            cur_tok = 0
    _emit(cur)
    return chunks


# Per-chunk MAP prompt. Asks for a small, speaker-attributed JSON digest of
# one slice of the meeting — faithful, no fabrication. The reduce step
# consumes these (not the raw transcript). The time range lets reduce order
# things across chunks.
MEETING_OPS_MAP_PROMPT = """You are analyzing ONE PORTION (chunk {n} of {total}, covering roughly {start}–{end} of the meeting) of a longer meeting transcript. Summarize ONLY what appears in this chunk — do not speculate about other parts of the meeting.

{speaker_context}

Rules:
- Use ONLY information explicitly stated in this chunk. Never fabricate.
- Attribute every point, decision, and quote to the speaker who said it, exactly as labeled in the transcript. Never output machine codes like SPEAKER_00.
- Quote only words that actually appear below. If a section has nothing, return an empty list.

---
TRANSCRIPT (chunk {n} of {total}):
{transcript}

---
Output ONLY a single fenced JSON block (no prose before or after) with this exact shape:

```json
{{"key_points": ["point (Speaker Name)", ...],
  "decisions": ["decision (decided by Speaker)", ...],
  "action_items": [{{"task": "...", "assignee": "Speaker or Not specified"}}],
  "quotes": [{{"text": "exact words from this chunk", "speaker": "Speaker Name"}}],
  "open_questions": ["..."],
  "notable": ["any other substantive context worth carrying forward"]}}
```
"""


# REDUCE prompt. Adapts the full single-call summarizer prompt: instead of a
# raw transcript it receives the structured chunk digests, and it must
# synthesize across the WHOLE meeting, dedupe, and keep speaker names. The
# output sections + fenced JSON are IDENTICAL to MEETING_OPS_SUMMARIZER_STYLE_PROMPT
# so the reduce response flows through the same parse/write block unchanged.
MEETING_OPS_REDUCE_PROMPT = """You are synthesizing the FINAL notes for a long meeting that was analyzed in {total} sequential chunks. Below are the structured, speaker-attributed digests of each chunk, in chronological order. Together they cover the ENTIRE meeting.

{speaker_context}

Synthesize a SINGLE coherent set of meeting notes spanning the whole meeting:
- Merge and DEDUPLICATE points, decisions, action items, and quotes that recur across chunks (the chunks overlap slightly, so the same item may appear more than once — report it once).
- Preserve speaker attribution exactly as given in the digests. Never output machine codes like SPEAKER_00, and never invent speakers or details not present in the digests.
- Order topics and events as they unfolded across the meeting (the digests are in order).

Output the following sections in order. Include ALL substantive information from the digests.

## Executive Summary
Write 2-3 sentences capturing the meeting's main purpose, key outcomes, and overall tone — across the FULL meeting.

## Key Discussion Points
For each major topic discussed, create a numbered section:
1. **[Topic Title]**
   - [Key point] ([Speaker Name] raised this)
   - [Supporting detail or counterpoint] ([Speaker Name])
   Include all perspectives shared, areas of agreement, and disagreements.

## Action Items
Create a markdown table with these columns:
| Owner | Action | Priority | Deadline |
For each action item explicitly stated. If owner/deadline not stated, write "Not specified."

## Key Decisions
List each decision made during the meeting:
- **[Decision]** - Made by [who], rationale: [why, if discussed]

## Key Quotes
Select up to 5 notable quotes that capture important positions, commitments, or insights:
> "[Exact words from a digest]" - [Speaker Name]
> Context: [One sentence explaining why this quote matters]

## Next Steps
- [Next step with responsible person if mentioned]

## Open Questions
- [Any unresolved items that need follow-up]

IMPORTANT: If a section has no genuine content across the digests, write "None identified." under that heading — never invent, pad, or infer items to fill a section. Quote only words that appear in the digests.

---
CHUNK DIGESTS:
{digests}

---
After the markdown sections above, emit a single fenced JSON block with the same content split into structured keys. This is required for downstream tooling. The fenced block looks like:

```json
{{"executive": "...",
  "bullets": ["topic 1: point", "topic 2: point", ...],
  "actions": [{{"action": "...", "owner": "...", "priority": "high|medium|low", "deadline": "..."}}],
  "decisions": ["decision (decided by Speaker)", ...],
  "questions": ["..."],
  "quotes": [{{"text": "...", "speaker": "...", "context": "..."}}],
  "next_steps": ["..."],
  "minutes": "the full markdown output above as a single string",
  "title": "short descriptive title for this meeting"}}
```
"""


async def _summarize_mapreduce(
    llm,
    *,
    system_prompt: str,
    speaker_context: str,
    chunks: list[dict[str, Any]],
    style_prefix: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    """Run the map (per-chunk) + reduce (synthesis) passes. Returns
    ``(reduce_text, missing)`` where reduce_text is the SAME fenced markdown +
    JSON a single call returns (so the caller feeds it through the existing
    parse/write block) and ``missing`` lists the chunks the map step dropped
    (``[{"index", "range"}]``) so the caller can stamp summary_truncated.

    Map chunk-calls are independent and run with bounded concurrency
    (semaphore of 3). Results are reassembled in chunk order before reduce.
    Raises on any unrecoverable failure (no usable map output, or the reduce
    LLM call returns empty); the caller catches and falls back to the
    single-call path.
    """
    total = len(chunks)
    extra = {
        "top_p": 0.9,
        "top_k": 30,
        "presence_penalty": 1.2,
        "repetition_penalty": 1.0,
    }
    sem = asyncio.Semaphore(3)

    async def _map_one(chunk: dict[str, Any]) -> Optional[dict[str, Any]]:
        prompt = MEETING_OPS_MAP_PROMPT.format(
            n=chunk["index"] + 1,
            total=total,
            start=_fmt_clock(chunk["start"]),
            end=_fmt_clock(chunk["end"]),
            speaker_context=speaker_context,
            transcript=chunk["text"],
        )
        if style_prefix:
            prompt = f"{style_prefix}\n\n{prompt}"
        try:
            async with sem:
                text = await asyncio.wait_for(
                    llm.chat(
                        system_prompt=system_prompt,
                        user_prompt=prompt,
                        max_tokens=4096,
                        temperature=0.6,
                        extra_params=extra,
                    ),
                    timeout=300,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("map-reduce: chunk %d/%d LLM call failed: %s", chunk["index"] + 1, total, exc)
            return None
        data = _parse_summarizer_json(text, fallback_title="")
        return {
            "index": chunk["index"],
            "range": f"{_fmt_clock(chunk['start'])}–{_fmt_clock(chunk['end'])}",
            "speakers": chunk.get("speakers") or [],
            "key_points": data.get("key_points") or [],
            "decisions": data.get("decisions") or [],
            "action_items": data.get("action_items") or [],
            "quotes": data.get("quotes") or [],
            "open_questions": data.get("open_questions") or data.get("questions") or [],
            "notable": data.get("notable") or [],
        }

    results = await asyncio.gather(*(_map_one(c) for c in chunks))
    digests = [r for r in results if r is not None]
    if not digests:
        raise RuntimeError("map-reduce produced no usable chunk digests")
    digests.sort(key=lambda d: d["index"])

    # Coverage: which chunks the map step dropped (LLM failure / unparseable).
    # The reduce proceeds with what succeeded, but the caller MUST be able to
    # stamp summary_truncated + the missing ranges rather than claim full
    # coverage — a partial outage silently omitting time ranges is the audit's
    # "long-meeting summary correctness" finding.
    covered = {d["index"] for d in digests}
    missing = [
        {"index": c["index"],
         "range": f"{_fmt_clock(c['start'])}–{_fmt_clock(c['end'])}"}
        for c in sorted(chunks, key=lambda c: c["index"])
        if c["index"] not in covered
    ]
    if missing:
        logger.warning(
            "map-reduce: %d/%d chunk(s) dropped — summary will be PARTIAL: %s",
            len(missing), total, ", ".join(m["range"] for m in missing),
        )

    # Serialize the digests compactly for the reduce prompt. They are small
    # (a few hundred tokens each), so the reduce call stays well within one
    # chunk's budget even for very long meetings.
    digest_blocks: list[str] = []
    for d in digests:
        block = {k: d[k] for k in (
            "key_points", "decisions", "action_items", "quotes",
            "open_questions", "notable",
        )}
        digest_blocks.append(
            f"### Chunk {d['index'] + 1} of {total} ({d['range']}) — "
            f"speakers: {', '.join(d['speakers']) if d['speakers'] else 'n/a'}\n"
            + json.dumps(block, ensure_ascii=False)
        )
    digests_text = "\n\n".join(digest_blocks)

    reduce_prompt = MEETING_OPS_REDUCE_PROMPT.format(
        total=total,
        speaker_context=speaker_context,
        digests=digests_text,
    )
    if style_prefix:
        reduce_prompt = f"{style_prefix}\n\n{reduce_prompt}"

    reduce_text = await asyncio.wait_for(
        llm.chat(
            system_prompt=system_prompt,
            user_prompt=reduce_prompt,
            max_tokens=8192,
            temperature=0.6,
            extra_params=extra,
        ),
        timeout=420,
    )
    if not (reduce_text or "").strip():
        raise RuntimeError("map-reduce reduce step returned empty output")
    return reduce_text, missing


async def _summarize_session(
    db: Session,
    session: RecordingSession,
    *,
    template: str = "standard",
    llm_model: Optional[str] = None,
    llm_thinking: Optional[bool] = None,
    force: bool = False,
) -> None:
    # Prefer a speaker-ATTRIBUTED transcript built from the diarized
    # segments ("Aaron Stransky: …" / "Speaker 1: …" lines). The old path
    # fed the flat transcript_simple (no per-line attribution at all) plus a
    # "here are the speaker labels, attribute statements to them" instruction
    # — so the model had to GUESS who said what, and raw pyannote codes
    # (SPEAKER_00) leaked straight into summaries. Raw codes are normalized
    # to "Speaker N" here even if the session predates label normalization.
    from services.speaker_labels import build_attributed_transcript, normalized_label_map

    transcript = ""
    speakers_seen: list[str] = []
    # Kept for the map-reduce path: the raw diarized speaker-turns and the
    # ONE shared label map, so chunking renders every chunk with the same
    # "Speaker N" numbering the whole-transcript render used (recomputing the
    # map per chunk would renumber speakers differently across chunks).
    segments: list[dict[str, Any]] = []
    label_map: Optional[dict[str, str]] = None
    diarized = session.transcript_diarized
    if isinstance(diarized, dict) and diarized.get("segments"):
        segments = diarized.get("segments") or []
        label_map = normalized_label_map([s.get("speaker") or "" for s in segments])
        transcript, speakers_seen = build_attributed_transcript(segments, label_map)
    if not transcript.strip():
        transcript = session.transcript_simple or ""
    if not transcript.strip():
        session.summary = json.dumps({"executive": "No transcript text was available.", "bullets": [], "actions": [], "decisions": []})
        db.commit()
        return

    style = _SUMMARY_TEMPLATE_GUIDANCE.get(template, _SUMMARY_TEMPLATE_GUIDANCE["standard"])

    # Per-upload override wins, then env-configured direct route, then the
    # org's ProviderRegistry default.
    direct = None if llm_model else _direct_summarizer_provider()
    if direct is not None:
        llm = direct
        # Use the deploy-doc system prompt verbatim when on the direct
        # route — that's the prompt David and Aaron specced for the
        # midboy1:8088 Qwen path.
        system_prompt = MEETING_OPS_SUMMARIZER_SYSTEM_PROMPT
    else:
        from services.providers.registry import get_provider_registry
        registry = get_provider_registry(db)
        llm = registry.get_llm(
            session.organization_id,
            task="quality",
            model_override=llm_model,
            thinking_override=llm_thinking,
        )
        system_prompt = (
            f"You are a meeting analyst. {style} Produce concise, useful meeting summaries."
        )

    # Speaker context: the transcript lines above are already prefixed with
    # the speaker ("Name: …"), so attribution is read, not guessed. Identified
    # speakers show real names; unidentified ones show "Speaker N". The model
    # must never emit raw diarizer codes or invent identities.
    if speakers_seen:
        named = [s for s in speakers_seen if not re.match(r"^Speaker \d+$", s)]
        generic = [s for s in speakers_seen if re.match(r"^Speaker \d+$", s)]
        parts = [
            "Each transcript line is prefixed with its speaker. Speakers: "
            + ", ".join(speakers_seen) + "."
        ]
        if generic:
            parts.append(
                " " + ", ".join(generic)
                + (" is an unidentified participant" if len(generic) == 1
                   else " are unidentified participants")
                + " — refer to them exactly by that label; do NOT guess their identity."
            )
        if named:
            parts.append(
                " Attribute statements only as written in the transcript."
            )
        parts.append(
            " Never output machine codes like SPEAKER_00 — use only the labels above."
        )
        speaker_context = "".join(parts)
    else:
        speaker_context = "Speaker labels are not available; describe statements neutrally and never invent speaker names."

    # v3.35.0 (audit #6): short recordings / single-speaker memos get the
    # lighter memo prompt — the 7-section format forces padded, fabricated-
    # looking minutes on a 60-second voice memo. Same fenced-JSON contract.
    word_count = len(transcript.split())
    is_memo = word_count < 300 or len(speakers_seen) <= 1

    # Transcript caps (audit #7). Direct route = the dedicated Qwen 3.6
    # endpoint (256K ctx) → 200K chars (~50K tokens) covers ~5h of dense
    # talk. Registry route stays 60K (model unknown — could be anything an
    # org configured). Either way, truncation is DISCLOSED to the model and
    # stamped into processing_metadata — never silently dropped (the old
    # comment claimed 60K ≈ "~3 hours"; it's really ~75-90 min).
    cap = 200_000 if direct is not None else 60_000
    truncated = len(transcript) > cap
    # Map-reduce time-ranges omitted by dropped chunks (audit: long-meeting
    # summary correctness). Empty on the single-call path.
    summary_missing_ranges: list[str] = []
    transcript_body = transcript[:cap]
    if truncated:
        pct = int(100 * cap / max(len(transcript), 1))
        transcript_body += (
            f"\n\n[NOTE: transcript truncated — the text above covers roughly the "
            f"first {pct}% of the meeting. State in the Executive Summary that "
            f"this summary covers only that portion.]"
        )

    # v3.35.0 (audit #8): the full structured prompt ships on BOTH routes —
    # it's model-agnostic markdown+JSON. Previously the registry route used a
    # stripped ask (executive/bullets/actions/decisions only), so quotes /
    # next_steps / questions / minutes were permanently empty on any deploy
    # without the direct env, and the UI sections silently varied.
    body_template = (
        MEETING_OPS_MEMO_PROMPT if is_memo else MEETING_OPS_SUMMARIZER_STYLE_PROMPT
    )
    prompt = body_template.format(
        speaker_context=speaker_context,
        transcript=transcript_body,
    )
    if direct is None:
        # Keep the org-template guidance on the registry route (the only
        # intended per-route difference now).
        prompt = f"{style}\n\n{prompt}"

    # v3.29.1 idempotency guard: skip the LLM call when the EXACT summary
    # input (system + body prompt — which fold in transcript_simple, the
    # speaker labels, the template, and which route/prompt was chosen) is
    # byte-identical to the input that produced the CURRENT stored summary.
    # This catches the genuinely redundant case — a reprocess whose
    # re-transcription + diarization came out identical to the last pass —
    # WITHOUT ever skipping a summary the inputs actually changed (new
    # transcript, new template, or newly-identified speaker names all shift
    # the hash and re-run). `force=True` (an explicit "regenerate") always
    # re-runs. The browser-live -> server-final progression is unaffected:
    # those prompts differ (browser STT text vs Parakeet text), so both run.
    summary_input_hash = hashlib.sha256(
        f"{template}\x00{system_prompt}\x00{prompt}".encode("utf-8")
    ).hexdigest()
    if not force and session.summary:
        existing_md = session.processing_metadata or {}
        if existing_md.get("summary_input_hash") == summary_input_hash:
            logger.info(
                "Summary input unchanged for session=%s — skipping redundant LLM call",
                session.id,
            )
            return

    # Map-reduce decision (v3.36.0). Engage ONLY when the toggle is on AND the
    # attributed transcript is longer than one chunk AND there are real
    # speaker-turns to split on AND this isn't a short memo. Otherwise the
    # single-call path below runs UNCHANGED — the short/one-chunk case must be
    # byte-for-byte the current behavior, so it is never routed through the new
    # prompts. On any map-reduce failure we fall through to the single call,
    # which keeps `truncated` honest (the truncated body is what it summarized).
    chunk_tokens = _mapreduce_chunk_tokens()
    summary_mode = "single"
    summary_chunks = 0
    text: Optional[str] = None
    if (
        _mapreduce_enabled()
        and not is_memo
        and segments
        and _estimate_tokens(transcript) > chunk_tokens
    ):
        chunks = _chunk_segments(
            segments,
            label_map,
            chunk_tokens=chunk_tokens,
            overlap_tokens=_mapreduce_overlap_tokens(),
        )
        if len(chunks) > 1:
            try:
                logger.info(
                    "Summarizing session=%s via map-reduce: %d chunks (~%d tokens, chunk_tokens=%d)",
                    session.id, len(chunks), _estimate_tokens(transcript), chunk_tokens,
                )
                text, mr_missing = await _summarize_mapreduce(
                    llm,
                    system_prompt=system_prompt,
                    speaker_context=speaker_context,
                    chunks=chunks,
                    # Mirror the single-call route difference: registry route
                    # carries the org-template guidance prefix, direct route
                    # does not (direct uses the verbatim spec'd system prompt).
                    style_prefix=(style if direct is None else ""),
                )
                summary_mode = "mapreduce"
                summary_chunks = len(chunks)
                # Honest coverage: full only when NO chunk was dropped. A partial
                # outage that omitted time ranges must stamp truncated + ranges.
                if mr_missing:
                    truncated = True
                    summary_missing_ranges = [m["range"] for m in mr_missing]
                else:
                    truncated = False  # full coverage — every chunk was summarized
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "map-reduce summarization failed for session=%s (%s) — "
                    "falling back to single-call (truncated) path",
                    session.id, exc,
                )
                text = None

    # Single-call path (default, and the fallback). Qwen3.6 inference params
    # per the model card + Meeting Minutes tuning: temperature 0.6, top_p 0.9,
    # top_k 30, presence_penalty 1.2, repetition_penalty 1.0. These reduce both
    # repetition loops (the Qwen model card explicitly warns greedy decoding
    # causes loops) and off-topic drift. Pass them through extra_params so
    # generic OpenAI-compatible endpoints get them too.
    if text is None:
        text = await asyncio.wait_for(
            llm.chat(
                system_prompt=system_prompt,
                user_prompt=prompt,
                max_tokens=8192,
                temperature=0.6,
                extra_params={
                    "top_p": 0.9,
                    "top_k": 30,
                    "presence_penalty": 1.2,
                    "repetition_penalty": 1.0,
                },
            ),
            timeout=420,
        )
    if not (text or "").strip():
        from services.providers.impl_llm import LLMUnavailable

        raise LLMUnavailable("Summary generation returned no content")
    analysis = _parse_summarizer_json(text, fallback_title=session.title or "")
    # executive must be a real summary sentence — never empty and never a bare
    # name/title (the memo prompt occasionally dropped the speaker's name or
    # nothing into it). Guarantee a usable value: prefer the LLM's executive,
    # else the markdown "## Summary" section, else the first key points, else
    # the title. A value under ~6 words is treated as name/title-like.
    _exec = (analysis.get("executive") or "").strip()
    if not _exec or len(_exec.split()) < 6:
        _fallback = (
            _extract_summary_section(text)
            or " ".join(str(b).strip() for b in (analysis.get("bullets") or [])[:2]).strip()
            or (analysis.get("title") or session.title or "").strip()
        )
        if _fallback:
            _exec = _fallback
    analysis["executive"] = _exec
    session.summary = json.dumps(analysis)
    session.final_summary = {
        "executive": _exec,
        "bullets": analysis.get("bullets", []),
        "actions": analysis.get("actions", []),
        "decisions": analysis.get("decisions", []),
        "questions": analysis.get("questions", []),
        "quotes": analysis.get("quotes", []),
        "next_steps": analysis.get("next_steps", []),
        "minutes": analysis.get("minutes") or _markdown_from_response(text),
        "title": analysis.get("title", session.title),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # ai_insights is owned by api.ai_insights and stored under a
    # MeetingInsightsResponse schema. Writing the summarizer's shape
    # here used to fail Pydantic validation on every /insights GET and
    # forced a fresh LLM regeneration every page load.
    new_title = analysis.get("title")
    if isinstance(new_title, str) and not getattr(session, "title_user_set", False):
        new_title = new_title.strip()
        if new_title and new_title.lower() not in {"untitled", "meeting", "recording"}:
            session.title = new_title[:200]

    # Promote action items out of the JSON column into the first-class
    # `action_items` table so the dashboard + PWA notifications can
    # read them directly. Re-process = fresh slate (delete + insert).
    try:
        from services.action_items_extractor import persist_action_items
        persist_action_items(db, session)
    except Exception as exc:  # noqa: BLE001
        # Action-item promotion is best-effort; never block the summary
        # commit on it. Falls back to the old in-JSON path which the
        # frontend parser still understands.
        logger.warning("action_items promotion failed for session %s: %s", session.id, exc)

    # Stamp the input hash so a later identical re-summarize (e.g. a no-op
    # reprocess) short-circuits at the guard above. Only reached on a
    # successful LLM summary — a failed/empty-transcript path returns earlier
    # and never poisons the hash.
    summary_md = dict(session.processing_metadata or {})
    summary_md["summary_input_hash"] = summary_input_hash
    # Truncation disclosure (v3.35.0, audit #7): record when the summary saw
    # only part of the transcript so the UI/exports can surface it. Map-reduce
    # normally covers the whole meeting (summary_truncated=False), BUT a dropped
    # chunk makes coverage partial — then truncated=True and the omitted time
    # ranges are recorded (audit: long-meeting summary correctness).
    summary_md["summary_truncated"] = bool(truncated)
    if summary_missing_ranges:
        summary_md["summary_missing_ranges"] = summary_missing_ranges
    else:
        summary_md.pop("summary_missing_ranges", None)
    summary_md["summary_prompt_variant"] = "memo" if is_memo else "full"
    summary_md["summary_mode"] = summary_mode
    if summary_mode == "mapreduce":
        summary_md["summary_chunks"] = summary_chunks
    session.processing_metadata = summary_md
    flag_modified(session, "processing_metadata")

    db.commit()
    db.refresh(session)


def _find_session(db: Session, session_id: str, org_id: int) -> Optional[RecordingSession]:
    query = db.query(RecordingSession).filter(RecordingSession.organization_id == org_id)
    session = query.filter(RecordingSession.session_id == session_id).first()
    if session:
        return session
    try:
        return query.filter(RecordingSession.id == int(session_id)).first()
    except (TypeError, ValueError):
        return None


@ws_router.websocket("/ws/uploads/{job_id}")
async def uploads_websocket(websocket: WebSocket, job_id: str):
    ok, user = await enforce_ws_auth(websocket)
    if not ok:
        return
    db = SessionLocal()
    try:
        try:
            uid = uuid.UUID(job_id)
        except ValueError:
            await websocket.close(code=1008)
            return
        query = db.query(UploadJob).filter(UploadJob.upload_id == uid)
        if user is not None and not getattr(user, "is_superuser", False):
            query = query.filter(
                UploadJob.organization_id.in_(getattr(user, "_org_ids", [])),
                UploadJob.user_id == user.id,
            )
        job = query.first()
        if not job:
            await websocket.close(code=1008)
            return
        await ws_manager.connect(job_id, websocket)
        await websocket.send_json(_status_payload(job))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(job_id, websocket)
        db.close()
