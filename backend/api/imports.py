"""Bulk audio import HTTP endpoints.

The /import page POSTs against these endpoints to drive a bulk ingest:

  POST   /api/import/jobs                  create an empty job
  POST   /api/import/jobs/{job_id}/files   upload one file -> queue it
  GET    /api/import/jobs/{job_id}         job + per-file status
  POST   /api/import/jobs/{job_id}/cancel  soft-cancel; queued rows skip

Auth is the same Keycloak SSO + JWT + API key chain the rest of the app
uses (auth.dependencies.get_current_user). All endpoints are org-scoped
via get_current_organization — cross-org leaks return 404 (not 403, by
the project convention from test_cross_org_isolation: a user has no way
to know whether a job in another org even exists).

Per-file pipeline runs out-of-band in services.bulk_import_queue.
Submit-and-forget: the upload handler returns as soon as the bytes are
on disk + the row is created + the queue has the file_id. No part of
the user-facing request blocks on Parakeet.

Per the design doc (`docs/bulk-audio-import-design.md`), this is the
Phase 1 minimal API. Pause/resume + admin controls land in B-import.4.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from auth.tier import gate_feature_for_caller
from database.database import get_db
from database.models import BulkImportFile, BulkImportJob
from services.bulk_import_queue import bulk_import_queue, _bulk_dir
from utils.filename_parser import parse_filename

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/import", tags=["Bulk Import"])


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _default_max_file_bytes() -> int:
    """Per-file upload cap. Defaults to UPLOAD_MAX_FILE_BYTES (the same
    knob the single-file upload uses) so bulk imports inherit the
    operator's tuning. Falls back to 500 MiB if neither knob is set."""
    raw = os.getenv("BULK_IMPORT_MAX_FILE_BYTES") or os.getenv(
        "UPLOAD_MAX_FILE_BYTES", str(500 * 1024 * 1024)
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 500 * 1024 * 1024


# ---------------------------------------------------------------------------
# Pydantic response shapes
# ---------------------------------------------------------------------------


class JobCreateResponse(BaseModel):
    job_id: str
    status: str


class FileUploadResponse(BaseModel):
    file_id: str
    job_id: str
    original_filename: str
    parsed_title: Optional[str] = None
    parsed_date: Optional[date] = None
    parsed_time: Optional[time] = None
    parsed_source: Optional[str] = None
    parsed_confidence: Optional[float] = None
    bytes_total: Optional[int] = None
    status: str


class JobStatusFile(BaseModel):
    file_id: str
    original_filename: str
    parsed_title: Optional[str] = None
    parsed_date: Optional[date] = None
    parsed_time: Optional[time] = None
    parsed_confidence: Optional[float] = None
    status: str
    session_id: Optional[str] = None
    error_message: Optional[str] = None
    bytes_total: Optional[int] = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    total_files: int
    succeeded: int
    failed: int
    skipped: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    created_at: datetime
    files: list[JobStatusFile]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_job_for_user(
    db: Session,
    job_id: uuid.UUID,
    user: User,
    org: ActiveOrganization,
) -> BulkImportJob:
    """Org-scoped job lookup. Returns 404 on miss + on cross-org so the
    caller can't probe for the existence of jobs in another org.

    Superuser users can read any org's jobs — same precedent the
    existing upload + session endpoints set."""
    job = (
        db.query(BulkImportJob).filter(BulkImportJob.id == job_id).first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Bulk import job not found.")
    if (
        job.organization_id != org.organization.id
        and not user.is_superuser
    ):
        # Cross-org probe: same 404 as missing so we don't leak existence.
        raise HTTPException(status_code=404, detail="Bulk import job not found.")
    return job


def _serialize_job(job: BulkImportJob, files: list[BulkImportFile]) -> JobStatusResponse:
    return JobStatusResponse(
        job_id=str(job.id),
        status=job.status,
        total_files=int(job.total_files or 0),
        succeeded=int(job.succeeded or 0),
        failed=int(job.failed or 0),
        skipped=int(job.skipped or 0),
        started_at=job.started_at,
        finished_at=job.finished_at,
        cancelled_at=job.cancelled_at,
        created_at=job.created_at,
        files=[
            JobStatusFile(
                file_id=str(f.id),
                original_filename=f.original_filename,
                parsed_title=f.parsed_title,
                parsed_date=f.parsed_date,
                parsed_time=f.parsed_time,
                parsed_confidence=f.parsed_confidence,
                status=f.status,
                session_id=(
                    str(f.session_id_when_created)
                    if f.session_id_when_created
                    else None
                ),
                error_message=f.error_message,
                bytes_total=f.bytes_total,
            )
            for f in files
        ],
    )


def _safe_ext(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in {
        "m4a", "mp3", "wav", "flac", "ogg", "opus", "aac",
        "mp4", "mov", "mkv", "webm", "avi",
    }:
        return suffix
    return "bin"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/jobs", response_model=JobCreateResponse)
async def create_job(
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> JobCreateResponse:
    """Create an empty bulk import job. Returns job_id which the client
    then POSTs files into."""
    gate_feature_for_caller(current_user, "bulk_import", active_org)  # v3.0.0 tier gate: server batch ingest is paid-tier
    job = BulkImportJob(
        id=uuid.uuid4(),
        user_id=current_user.id,
        organization_id=active_org.organization.id,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return JobCreateResponse(job_id=str(job.id), status=job.status)


@router.post("/jobs/{job_id}/files", response_model=FileUploadResponse)
async def upload_file_to_job(
    job_id: uuid.UUID,
    audio: UploadFile = File(...),
    override_title: Optional[str] = Form(None),
    override_meeting_date: Optional[str] = Form(None),
    override_meeting_time: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> FileUploadResponse:
    """Upload one audio file to a job.

    Streams the multipart body to disk under
    `RECORDINGS_DIR/bulk-import/{job_id}/{file_id}.{ext}` so we never
    slurp a 50 MB voice memo into memory. Parses the filename so the
    response carries title + date + time the client renders in its
    preview row. Then submits the file_id to the queue.

    The queue worker (services.bulk_import_queue) is the only thing that
    runs the SHA-256 + dedup + RecordingSession create + reprocess
    pipeline; this endpoint just lands the bytes + the row.
    """
    gate_feature_for_caller(current_user, "bulk_import", active_org)  # v3.0.0 tier gate: server batch ingest is paid-tier
    job = _get_job_for_user(db, job_id, current_user, active_org)
    if job.status in ("complete", "cancelled", "failed"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is already {job.status}; cannot add files.",
        )

    # Block uploads to a job owned by a different user in the same org.
    # The job is org-scoped, but a job created by user A should not be
    # appended to by user B even if both are in the org — collaboration
    # on bulk-imports lands later. Superuser bypasses for support.
    if job.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="Bulk import job belongs to another user.",
        )

    original_filename = (audio.filename or "upload.bin").strip()
    if not original_filename:
        raise HTTPException(status_code=400, detail="filename missing.")

    file_id = uuid.uuid4()
    ext = _safe_ext(original_filename)
    target_dir = _bulk_dir(job.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{file_id}.{ext}"

    # Stream the upload to disk in 8 MiB chunks. UploadFile.file is a
    # SpooledTemporaryFile under the hood; reading it bounded keeps
    # peak RAM small even for large m4a voice memos.
    max_bytes = _default_max_file_bytes()
    bytes_written = 0
    try:
        with target_path.open("wb") as out:
            while True:
                chunk = await audio.read(8 * 1024 * 1024)
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    out.close()
                    try:
                        target_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File exceeds the bulk import per-file limit "
                            f"of {max_bytes} bytes."
                        ),
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as exc:
        try:
            target_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.exception("bulk-import: file write failed: %s", exc)
        raise HTTPException(
            status_code=500, detail=f"Failed to persist upload: {exc}"
        )

    # Parse filename + honor overrides. Overrides are strings on the
    # multipart form; coerce date/time defensively so a malformed input
    # falls back to the parser's guess rather than 500'ing.
    parsed = parse_filename(original_filename)

    chosen_title = (override_title or parsed.title or "")[:200] or None
    chosen_date: Optional[date] = parsed.meeting_date
    if override_meeting_date:
        try:
            chosen_date = date.fromisoformat(override_meeting_date.strip())
        except ValueError:
            pass  # keep parser value
    chosen_time: Optional[time] = parsed.meeting_time
    if override_meeting_time:
        try:
            # Accept HH:MM or HH:MM:SS
            chosen_time = time.fromisoformat(override_meeting_time.strip())
        except ValueError:
            pass

    file_row = BulkImportFile(
        id=file_id,
        job_id=job.id,
        original_filename=original_filename,
        parsed_title=chosen_title,
        parsed_date=chosen_date,
        parsed_time=chosen_time,
        parsed_source=parsed.source,
        parsed_confidence=parsed.confidence,
        status="queued",
        bytes_total=bytes_written,
    )
    db.add(file_row)
    job.total_files = (job.total_files or 0) + 1
    db.commit()
    db.refresh(file_row)

    # Submit to the queue. If the queue isn't running (test fixture, or
    # process startup race) the API still returns 200 — the queue's
    # start() recovery scan picks the row up on next boot.
    try:
        await bulk_import_queue.submit(file_row.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "bulk-import: queue.submit failed for file=%s: %s", file_id, exc
        )

    return FileUploadResponse(
        file_id=str(file_row.id),
        job_id=str(job.id),
        original_filename=original_filename,
        parsed_title=chosen_title,
        parsed_date=chosen_date,
        parsed_time=chosen_time,
        parsed_source=parsed.source,
        parsed_confidence=parsed.confidence,
        bytes_total=bytes_written,
        status=file_row.status,
    )


@router.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> JobStatusResponse:
    """Returns job + per-file rows + counters.

    The frontend polls this every 5s while the job is in flight (or
    over WebSocket once B-import.2 lands a /ws/import/{job_id}
    endpoint). Read-only — no side effects.
    """
    job = _get_job_for_user(db, job_id, current_user, active_org)
    files = (
        db.query(BulkImportFile)
        .filter(BulkImportFile.job_id == job.id)
        .order_by(BulkImportFile.created_at.asc())
        .all()
    )
    return _serialize_job(job, files)


@router.post("/jobs/{job_id}/cancel", response_model=JobStatusResponse)
async def cancel_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> JobStatusResponse:
    """Soft-cancel a running job.

    Sets cancelled_at + status='cancelled' on the job row, marks every
    queued file 'skipped' (they never start processing), and leaves
    files already in 'processing' alone — the worker finishes them
    naturally so half-Parakeet'd audio doesn't poison the session row.

    Returns the updated job snapshot so the UI can flip immediately to
    the cancelled state without an extra GET.
    """
    job = _get_job_for_user(db, job_id, current_user, active_org)
    if job.status in ("complete", "cancelled", "failed"):
        # Already terminal — return the current state without mutating.
        files = (
            db.query(BulkImportFile)
            .filter(BulkImportFile.job_id == job.id)
            .order_by(BulkImportFile.created_at.asc())
            .all()
        )
        return _serialize_job(job, files)

    # Only the job owner or a superuser can cancel. Same precedent as
    # the upload + tts cancel paths.
    if job.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="Bulk import job belongs to another user.",
        )

    now = datetime.now(timezone.utc)
    job.cancelled_at = now
    job.status = "cancelled"

    # Skip every still-queued file. Files in 'uploading' (caller crashed
    # mid-stream) also get skipped — the worker would mark them failed
    # eventually when the stale file is detected, but cancelled-skip is
    # a tighter user-facing semantic.
    queued = (
        db.query(BulkImportFile)
        .filter(
            BulkImportFile.job_id == job.id,
            BulkImportFile.status.in_(("queued", "uploading")),
        )
        .all()
    )
    for f in queued:
        f.status = "skipped"
        f.error_message = "cancelled before processing"
        f.finished_at = now
        job.skipped = (job.skipped or 0) + 1

    # If there are no in-flight files left to wait on, finalize finished_at.
    in_flight = (
        db.query(BulkImportFile)
        .filter(
            BulkImportFile.job_id == job.id,
            BulkImportFile.status == "processing",
        )
        .count()
    )
    if in_flight == 0:
        job.finished_at = job.finished_at or now

    db.commit()

    files = (
        db.query(BulkImportFile)
        .filter(BulkImportFile.job_id == job.id)
        .order_by(BulkImportFile.created_at.asc())
        .all()
    )
    return _serialize_job(job, files)

@router.post("/jobs/{job_id}/files/{file_id}/retry", response_model=JobStatusResponse)
async def retry_file(
    job_id: uuid.UUID,
    file_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> JobStatusResponse:
    job = _get_job_for_user(db, job_id, current_user, active_org)
    file_row = (
        db.query(BulkImportFile)
        .filter(BulkImportFile.id == file_id, BulkImportFile.job_id == job.id)
        .first()
    )
    if not file_row:
        raise HTTPException(status_code=404, detail="File not found in this job.")
    if file_row.status != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry file with status '{file_row.status}'. Only 'failed' files can be retried.",
        )
    file_row.status = "queued"
    file_row.error_message = None
    file_row.started_at = None
    file_row.finished_at = None
    job.failed = max(0, (job.failed or 0) - 1)
    if job.status in ("complete", "cancelled", "failed"):
        job.status = "processing"
        job.finished_at = None
        job.cancelled_at = None
    db.commit()
    try:
        await bulk_import_queue.submit(file_row.id)
    except Exception as exc:
        logger.warning("bulk-import: retry submit failed for file=%s: %s", file_id, exc)
    files = (
        db.query(BulkImportFile)
        .filter(BulkImportFile.job_id == job.id)
        .order_by(BulkImportFile.created_at.asc())
        .all()
    )
    return _serialize_job(job, files)
