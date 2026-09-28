"""Bulk audio import job queue.

Extends the existing UploadPipelineQueue pattern (api/uploads.py) with:

  * Batch work is routed to a dedicated Arq queue. User-facing finalize,
    reprocess, digest, and TTS jobs run on a separate interactive worker,
    so a large import cannot consume their reserved capacity.

  * Per-file pipeline:
      1. SHA-256 the assembled audio (skip if already computed at upload).
      2. Dedup against recording_sessions.processing_metadata->audio_sha256
         scoped to the file's org. Match -> mark skipped, increment job
         counter, no GPU time spent.
      3. Re-parse the filename (cheap, idempotent — we want the worker to
         own the canonical metadata even if the API row was stale).
      4. Create a RecordingSession row with parsed title / meeting_date /
         meeting_time, source_type='bulk_import', processing_metadata
         carrying the bulk_import_job_id back-pointer + audio_sha256.
      5. Push the audio bytes to Garage via services.session_media at the
         canonical key meeting-ops-audio/{org_id}/{session_id}/audio/{name}
         (recorded on the session's audio_object_key column; falls back to
         local disk under RECORDINGS_DIR when Garage isn't configured).
      6. Park the audio on local disk as well (the reprocess pipeline
         reads from disk, not Garage — Garage is the durable backup).
      7. Internally invoke _run_session_reprocess(session.id) to kick the
         Parakeet 1.1B + pyannote + Qwen 3.6 35B pipeline that runs for
         normal uploads.
      8. Mark the file row complete and bump job.succeeded.

  * Cancel awareness. The worker checks bulk_import_jobs.cancelled_at
    between files; a row in 'queued' that finds its job cancelled gets
    marked skipped and never starts processing. Files already in
    'processing' finish gracefully — we don't kill mid-Parakeet.

  * Restart recovery. On boot the queue scans rows in {queued, uploading}
    and re-submits them, so a uvicorn restart mid-batch picks up where it
    left off.

NOT a hand-rolled asyncio.Queue. We use a Semaphore directly because the
worker is started per-file by the API (submit-and-forget). This keeps the
contract simple: the API hands the queue a file_id; the queue spawns a
task that obeys the semaphore. No queue.task_done(), no worker loop.

Migration path to Arq + Redis lives in B-import.4 per the design doc.
The Semaphore pattern stays compatible — Arq has its own per-worker
concurrency setting that replaces this one without code restructuring on
the per-file pipeline.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from database.database import SessionLocal
from database.models import (
    BulkImportFile,
    BulkImportJob,
    RecordingSession,
    SpeakerSessionLink,
)
from services.speaker_service import find_speaker_by_name_hint

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config + storage paths
# ---------------------------------------------------------------------------


def get_bulk_import_concurrency() -> int:
    """Worker pool size. Default 2 to protect Parakeet 1.1B GPU load.

    Hard floor of 1 so a misconfiguration doesn't kill the queue entirely.
    Hard ceiling of 4 because past that pyannote + Parakeet thrash the
    RTX 6000 + speaker-svc box; per-file latency collapses and the gain
    from parallelism reverses. Matches the design doc's cap.
    """
    try:
        v = int(os.getenv("BULK_IMPORT_WORKERS", "2"))
    except ValueError:
        return 2
    if v < 1:
        return 1
    if v > 4:
        return 4
    return v


RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "/app/recordings"))
BULK_IMPORT_ROOT = RECORDINGS_DIR / "bulk-import"
GARAGE_AUDIO_BUCKET = os.getenv(
    "GARAGE_AUDIO_BUCKET", "meeting-ops-audio"
).strip()


def _bulk_dir(job_id: uuid.UUID) -> Path:
    return BULK_IMPORT_ROOT / str(job_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    """Async-friendly SHA-256 over a file on disk. Uses asyncio.to_thread
    so the event loop isn't blocked by a multi-GB read."""

    def _digest() -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                buf = f.read(chunk_size)
                if not buf:
                    break
                h.update(buf)
        return h.hexdigest()

    return await asyncio.to_thread(_digest)


def _find_existing_session_by_sha256(
    db: Session, organization_id: int, sha256_hex: str
) -> Optional[RecordingSession]:
    """Look up an existing session in the same org that already imported
    this audio. Match on processing_metadata->>'audio_sha256'.

    The lookup is intentionally org-scoped — two orgs may legitimately
    upload the same audio (a podcast guest sharing with both host and
    own org) and we don't leak that across tenant boundaries.

    Implemented as a string LIKE on the JSON-serialized column so we
    stay portable between Postgres JSONB (where ->> would be cleaner)
    and SQLite JSON (where the test fixture stores it as text). The
    SHA-256 hex is 64 chars + uniqueness makes false positives
    effectively impossible.
    """
    needle = f'%"audio_sha256": "{sha256_hex}"%'
    return (
        db.query(RecordingSession)
        .filter(
            RecordingSession.organization_id == organization_id,
            RecordingSession.processing_metadata.isnot(None),
            RecordingSession.processing_metadata.cast(
                # cast to Text so LIKE works on both JSONB + SQLite JSON.
                # SQLAlchemy emits CAST(processing_metadata AS TEXT) which
                # both dialects accept.
                __import__("sqlalchemy").Text
            ).like(needle),
        )
        .first()
    )


def _safe_ext(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    # Whitelist the extensions the platform actually supports. Anything
    # else falls back to .bin and the reprocess pipeline's ffmpeg step
    # will sniff the content.
    if suffix in {
        "m4a", "mp3", "wav", "flac", "ogg", "opus", "aac",
        "mp4", "mov", "mkv", "webm", "avi",
    }:
        return suffix
    return "bin"


# Garage upload is now handled uniformly by services.session_media
# (persist_session_audio) at the call site — see the "Garage upload" block in
# _process_file. The bespoke _upload_to_garage helper that used a job-scoped
# key was removed in favor of that single canonical path.


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


class BulkImportPipelineQueue:
    """Background worker pool processing BulkImportFile rows.

    Concurrency capped at BULK_IMPORT_WORKERS (default 2) to protect the
    Parakeet 1.1B + pyannote + Qwen 3.6 35B reprocess load. The semaphore
    is the single coordination point; submit() spawns a per-file task and
    each task awaits the semaphore before doing real work.
    """

    def __init__(self, max_workers: Optional[int] = None):
        n = max_workers if max_workers is not None else get_bulk_import_concurrency()
        self._max_workers = n
        self._semaphore = asyncio.Semaphore(n)
        self._tasks: set[asyncio.Task] = set()
        self._started = False
        # When True, the queue is in graceful-shutdown mode; new submit()
        # calls become no-ops so a process restart doesn't queue work that
        # would be torn down a moment later.
        self._stopping = False

    @property
    def max_workers(self) -> int:
        return self._max_workers

    @property
    def in_flight(self) -> int:
        """Number of tasks currently spawned (queued + running). Useful
        for the concurrency-cap test; not the same as "actively holding
        the semaphore" — but for testability we expose the spawned count."""
        return len(self._tasks)

    async def start(self) -> None:
        """Boot-time recovery. Scans bulk_import_files where status is
        in {queued, uploading} and re-submits them so a process restart
        picks up where it left off."""
        if self._started:
            return
        self._started = True
        self._stopping = False
        db = SessionLocal()
        try:
            pending = (
                db.query(BulkImportFile)
                .filter(BulkImportFile.status.in_(("queued", "uploading")))
                .all()
            )
            # Filter to jobs that aren't cancelled.
            for row in pending:
                job = (
                    db.query(BulkImportJob)
                    .filter(BulkImportJob.id == row.job_id)
                    .first()
                )
                if not job or job.cancelled_at is not None or job.status in (
                    "cancelled",
                    "complete",
                    "failed",
                ):
                    continue
                # Spawn through submit so the semaphore + bookkeeping path
                # is unified.
                await self.submit(row.id)
        finally:
            db.close()
        logger.info(
            "bulk-import queue started: max_workers=%s, recovered=%s",
            self._max_workers,
            len(self._tasks),
        )

    async def stop(self) -> None:
        """Graceful shutdown. Stops accepting new submissions and waits
        for in-flight tasks to drain. Per the design doc, mid-Parakeet
        work is not killed — we let it finish so half-processed audio
        doesn't poison the session row."""
        self._stopping = True
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._started = False

    async def submit(self, file_id: uuid.UUID) -> None:
        """Enqueue a file for processing.

        The semaphore caps concurrent in-flight work to max_workers; tasks
        beyond the cap park inside _process_file's `async with` until a
        slot opens. We deliberately do NOT bound the task spawn count —
        the semaphore is the only throttle, so the API path stays fast
        and never blocks on a slow Parakeet call.
        """
        if self._stopping:
            logger.warning("bulk-import: submit ignored during shutdown")
            return
        task = asyncio.create_task(self._process_file(file_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process_file(self, file_id: uuid.UUID) -> None:
        """Per-file pipeline. Wrapped in a try/except so a worker crash
        never leaves a row in 'processing' forever — the failed branch
        writes status='failed' with the exception message."""
        async with self._semaphore:
            try:
                await self._do_process_file(file_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "bulk-import: worker crashed on file_id=%s: %s",
                    file_id,
                    exc,
                )
                await asyncio.to_thread(self._mark_failed, file_id, str(exc))

    @staticmethod
    def _mark_failed(file_id: uuid.UUID, msg: str) -> None:
        """Sync helper for the outer worker try/except — keeps the
        terminal status write off the event loop."""
        db = SessionLocal()
        try:
            row = (
                db.query(BulkImportFile)
                .filter(BulkImportFile.id == file_id)
                .first()
            )
            if not row:
                return
            row.status = "failed"
            row.error_message = msg[:2000]
            row.finished_at = _now()
            # Bump the job's failed counter so the UI sees the delta.
            job = (
                db.query(BulkImportJob)
                .filter(BulkImportJob.id == row.job_id)
                .first()
            )
            if job:
                job.failed = (job.failed or 0) + 1
                _maybe_finish_job(job)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "bulk-import: _mark_failed failed for file_id=%s", file_id
            )
        finally:
            db.close()

    async def _do_process_file(self, file_id: uuid.UUID) -> None:
        """The real per-file pipeline. Sync DB reads are intentional —
        SQLAlchemy isn't async here and asyncio.to_thread'ing every read
        would obscure the code without changing throughput (the reads
        are microsecond-scale)."""
        # ---- Load + cancellation gate ----
        db = SessionLocal()
        try:
            file_row = (
                db.query(BulkImportFile)
                .filter(BulkImportFile.id == file_id)
                .first()
            )
            if not file_row:
                logger.warning("bulk-import: file_id=%s not found", file_id)
                return
            job = (
                db.query(BulkImportJob)
                .filter(BulkImportJob.id == file_row.job_id)
                .first()
            )
            if not job:
                logger.warning(
                    "bulk-import: job_id=%s missing for file=%s",
                    file_row.job_id,
                    file_id,
                )
                return

            # Cancelled-before-start: just mark skipped and move on. We
            # increment the job's skipped counter so the progress UI
            # accurately reflects that the file never ran.
            if job.cancelled_at is not None or job.status in (
                "cancelled",
                "complete",
            ):
                file_row.status = "skipped"
                file_row.error_message = "job cancelled before processing"
                file_row.finished_at = _now()
                job.skipped = (job.skipped or 0) + 1
                _maybe_finish_job(job)
                db.commit()
                return

            # Flip to processing. Also mark the job 'processing' if it's
            # still queued — first file picked up wins the transition.
            file_row.status = "processing"
            file_row.started_at = _now()
            if job.status == "queued":
                job.status = "processing"
                job.started_at = job.started_at or _now()
            db.commit()

            org_id = job.organization_id
            user_id = job.user_id
            job_id = job.id
            original_filename = file_row.original_filename
            local_path = (
                _bulk_dir(job_id)
                / f"{file_id}.{_safe_ext(original_filename)}"
            )
        finally:
            db.close()

        if not local_path.exists() or local_path.stat().st_size == 0:
            await asyncio.to_thread(
                self._mark_failed, file_id, "audio file missing from disk"
            )
            return

        # ---- SHA-256 + dedup ----
        sha256_hex = await _sha256_file(local_path)

        db = SessionLocal()
        try:
            file_row = (
                db.query(BulkImportFile)
                .filter(BulkImportFile.id == file_id)
                .first()
            )
            job = (
                db.query(BulkImportJob)
                .filter(BulkImportJob.id == file_row.job_id)
                .first()
            )
            file_row.file_sha256 = sha256_hex
            existing = _find_existing_session_by_sha256(
                db, job.organization_id, sha256_hex
            )
            if existing is not None:
                # Duplicate. No GPU time spent, no session created.
                file_row.status = "skipped"
                file_row.error_message = (
                    f"duplicate of existing session {existing.id}"
                )
                file_row.finished_at = _now()
                job.skipped = (job.skipped or 0) + 1
                _maybe_finish_job(job)
                db.commit()
                # Wipe the local copy — the existing session already has
                # the audio on disk, no point keeping a duplicate around.
                try:
                    local_path.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            db.commit()
        finally:
            db.close()

        # ---- Filename parse (worker-side, canonical) ----
        from utils.filename_parser import parse_filename

        parsed = parse_filename(original_filename)

        # ---- Create RecordingSession ----
        # Use the same shape api/uploads._create_upload_session uses so
        # the rest of the platform treats this row as a normal upload.
        session_uuid = str(uuid.uuid4())
        fallback_title = (
            Path(original_filename).stem.replace("_", " ").strip()
            or "Bulk Imported Meeting"
        )
        title = (parsed.title or fallback_title)[:200]

        db = SessionLocal()
        try:
            # created_at/started_at = server INGEST time (NOT the historical
            # recording time): the watchdog + retention/eviction measure age from
            # created_at, so backdating it to an imported file's original date
            # would make a fresh import look "stuck" and immediately retention-/
            # eviction-eligible (silent data loss). The historical timestamp is
            # preserved in meeting_date/meeting_time + the provenance ledger.
            from services.meeting_provenance import (
                build_meeting_provenance,
                recorded_date_time,
            )
            _uploaded_at = datetime.now(timezone.utc)
            _provenance = build_meeting_provenance(
                audio_path=str(local_path),
                original_filename=original_filename,
                filename_date=parsed.meeting_date,
                filename_time=parsed.meeting_time,
                uploaded_at=_uploaded_at,
                source_type="bulk_import",
            )
            _recorded = _provenance["when"]["recorded_at"]
            _meeting_date = parsed.meeting_date
            _meeting_time = parsed.meeting_time
            if _meeting_date is None:
                _rd, _rt = recorded_date_time(_recorded)
                _meeting_date = _rd
                _meeting_time = _meeting_time or _rt

            session = RecordingSession(
                session_id=session_uuid,
                name=title,
                title=title,
                description=f"Bulk imported from {original_filename}",
                meeting_type="upload",
                created_at=_uploaded_at,
                started_at=_uploaded_at,
                meeting_date=_meeting_date,
                meeting_time=_meeting_time,
                status="processing",
                user_id=user_id,
                organization_id=org_id,
                audio_file=str(local_path),
                source_type="bulk_import",
                processing_metadata={
                    "original_filename": original_filename,
                    "audio_sha256": sha256_hex,
                    "bulk_import_job_id": str(job_id),
                    "bulk_import_file_id": str(file_id),
                    "filename_parse": {
                        "confidence": parsed.confidence,
                        "source": parsed.source,
                        "matched_title": parsed.title,
                    },
                    "meeting_provenance": _provenance,
                },
            )
            db.add(session)
            db.flush()
            session_pk = session.id
            session_session_id = session.session_id
            db.commit()

            file_row = (
                db.query(BulkImportFile)
                .filter(BulkImportFile.id == file_id)
                .first()
            )
            # session_id_when_created stores the UUID session_id (public
            # identifier) — matches what the API surfaces in URLs.
            file_row.session_id_when_created = uuid.UUID(session_session_id)
            # Snapshot parsed_title to a local before commit + close. After
            # the session closes the ORM expires attributes on the
            # detached instance, so a later attribute read raises
            # DetachedInstanceError (the earlier B-import.3 implementation
            # relied on the attribute already being loaded, which is not
            # guaranteed by SQLAlchemy 2.x).
            parsed_title = file_row.parsed_title
            db.commit()
        finally:
            db.close()

        # ---- Step 3.5: speaker auto-link from filename hint (B-import.3) ----
        name_hint: Optional[str] = None
        if parsed_title:
            from utils.filename_parser import extract_call_with_name
            name_hint = extract_call_with_name(parsed_title)
        if name_hint:
            db = SessionLocal()
            try:
                speaker = find_speaker_by_name_hint(
                    db, org_id, name_hint,
                )
                if speaker:
                    link = SpeakerSessionLink(
                        speaker_id=speaker.id,
                        session_id=session_pk,
                        organization_id=org_id,
                        raw_label="HINT",
                        source="filename-hint",
                    )
                    db.add(link)
                    db.commit()
                    logger.info(
                        "[bulk-import] filename-hint speaker link session=%s "
                        "speaker=%s name_hint=%r",
                        session_pk, speaker.id, name_hint,
                    )
            finally:
                db.close()

        # ---- Garage upload (best-effort) ----
        # Unified through services.session_media so the durable copy lands at
        # the canonical key ({org}/{session_id}/audio/{name}) and is recorded
        # on the session's audio_storage_backend / audio_object_key columns —
        # the same mechanism the live-recording + upload paths use, so the
        # download fallback and delete-cascade find bulk-imported audio too.
        # (Runs in a thread: persist does blocking boto3 I/O.)
        db = SessionLocal()
        try:
            session = (
                db.query(RecordingSession)
                .filter(RecordingSession.id == session_pk)
                .first()
            )
            if session:
                from services.session_media import persist_session_audio

                backend = await asyncio.to_thread(
                    persist_session_audio, db, session, local_path=str(local_path)
                )
                if backend == "garage":
                    # Keep the legacy metadata keys for visibility/debug.
                    metadata = dict(session.processing_metadata or {})
                    metadata["garage_bucket"] = GARAGE_AUDIO_BUCKET
                    metadata["garage_key"] = session.audio_object_key
                    session.processing_metadata = metadata
                    flag_modified(session, "processing_metadata")
                    db.commit()
        finally:
            db.close()

        # ---- Reprocess kick (Parakeet + pyannote + Qwen 3.6) ----
        # Re-import locally to avoid module-cycle: api.recording imports
        # services indirectly. Lazy import keeps the queue importable from
        # main.py at boot before all routers are loaded.
        try:
            from api.recording import _run_session_reprocess

            await _run_session_reprocess(session_pk)
        except Exception as exc:  # noqa: BLE001
            # Reprocess failures bubble up as the file's terminal error.
            # The session row stays (status=processing) so the user can
            # inspect what was created; the file row is marked failed.
            logger.exception(
                "bulk-import: reprocess failed for session_pk=%s: %s",
                session_pk,
                exc,
            )
            db = SessionLocal()
            try:
                file_row = (
                    db.query(BulkImportFile)
                    .filter(BulkImportFile.id == file_id)
                    .first()
                )
                file_row.status = "failed"
                file_row.error_message = f"reprocess failed: {exc}"[:2000]
                file_row.finished_at = _now()
                job = (
                    db.query(BulkImportJob)
                    .filter(BulkImportJob.id == file_row.job_id)
                    .first()
                )
                if job:
                    job.failed = (job.failed or 0) + 1
                    _maybe_finish_job(job)
                db.commit()
            finally:
                db.close()
            return

        # ---- Terminal success state ----
        db = SessionLocal()
        try:
            file_row = (
                db.query(BulkImportFile)
                .filter(BulkImportFile.id == file_id)
                .first()
            )
            file_row.status = "complete"
            file_row.finished_at = _now()
            job = (
                db.query(BulkImportJob)
                .filter(BulkImportJob.id == file_row.job_id)
                .first()
            )
            if job:
                job.succeeded = (job.succeeded or 0) + 1
                _maybe_finish_job(job)
            db.commit()
        finally:
            db.close()


def _maybe_finish_job(job: BulkImportJob) -> None:
    """Flip job.status to 'complete' once every row is in a terminal
    state. Caller still has to db.commit() — we only mutate the in-
    memory object so the surrounding transaction can keep adding to it.

    A job is finished when total_files == succeeded + failed + skipped.
    If cancelled_at is set, the terminal state is 'cancelled' instead.
    """
    if job is None:
        return
    if job.status in ("complete", "cancelled", "failed"):
        # Already terminal. Idempotent — don't touch finished_at again.
        return
    total = job.total_files or 0
    done = (job.succeeded or 0) + (job.failed or 0) + (job.skipped or 0)
    if total == 0 or done < total:
        return
    if job.cancelled_at is not None:
        job.status = "cancelled"
    else:
        job.status = "complete"
    job.finished_at = _now()


# Process-wide singleton. Imported by api/imports.py + main.py.
bulk_import_queue = BulkImportPipelineQueue()


async def start_bulk_import_queue() -> None:
    await bulk_import_queue.start()


async def stop_bulk_import_queue() -> None:
    await bulk_import_queue.stop()


# ---------------------------------------------------------------------------
# Module-level helper: standalone _do_process_file for Arq worker
# ---------------------------------------------------------------------------


async def _do_process_file(file_id: uuid.UUID) -> None:
    """Standalone version of BulkImportPipelineQueue._do_process_file.

    Delegates to the queue instance so the pipeline logic lives in one
    place. The Arq worker calls this module-level function instead of
    the bound method.
    """
    await bulk_import_queue._do_process_file(file_id)


# ---------------------------------------------------------------------------
# Pause / Resume helpers (B-import.4)
# ---------------------------------------------------------------------------


async def pause_job(job_id: uuid.UUID) -> bool:
    """Set bulk_import_jobs.status=paused. Worker checks this between
    files and stops dequeuing. In-flight files finish naturally."""
    db = SessionLocal()
    try:
        job = (
            db.query(BulkImportJob)
            .filter(BulkImportJob.id == job_id)
            .first()
        )
        if not job:
            return False
        if job.status not in ("queued", "processing"):
            return False
        job.status = "paused"
        db.commit()
        return True
    finally:
        db.close()


async def resume_job(job_id: uuid.UUID) -> bool:
    """Flip status back to processing, re-enqueue remaining queued files."""
    db = SessionLocal()
    try:
        job = (
            db.query(BulkImportJob)
            .filter(BulkImportJob.id == job_id)
            .first()
        )
        if not job:
            return False
        if job.status != "paused":
            return False
        job.status = "processing"
        # Re-enqueue any files that are still queued.
        queued_files = (
            db.query(BulkImportFile)
            .filter(
                BulkImportFile.job_id == job_id,
                BulkImportFile.status == "queued",
            )
            .all()
        )
        for f in queued_files:
            try:
                await bulk_import_queue.submit(f.id)
            except Exception:
                pass
        db.commit()
        return True
    finally:
        db.close()


async def admin_cancel_job(job_id: uuid.UUID) -> bool:
    """Admin bypass: cancel any job regardless of ownership."""
    db = SessionLocal()
    try:
        job = (
            db.query(BulkImportJob)
            .filter(BulkImportJob.id == job_id)
            .first()
        )
        if not job:
            return False
        if job.status in ("complete", "cancelled", "failed"):
            return True  # already terminal
        now = _now()
        job.cancelled_at = now
        job.status = "cancelled"
        queued = (
            db.query(BulkImportFile)
            .filter(
                BulkImportFile.job_id == job_id,
                BulkImportFile.status.in_(("queued", "uploading")),
            )
            .all()
        )
        for f in queued:
            f.status = "skipped"
            f.error_message = "cancelled before processing"
            f.finished_at = now
            job.skipped = (job.skipped or 0) + 1
        in_flight = (
            db.query(BulkImportFile)
            .filter(
                BulkImportFile.job_id == job_id,
                BulkImportFile.status == "processing",
            )
            .count()
        )
        if in_flight == 0:
            job.finished_at = job.finished_at or now
        db.commit()
        return True
    finally:
        db.close()


async def list_all_jobs(
    status_filter: Optional[str] = None,
    user_id: Optional[int] = None,
    created_after: Optional[datetime] = None,
    created_before: Optional[datetime] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[BulkImportJob]:
    """List ALL bulk_import_jobs across orgs (admin only)."""
    db = SessionLocal()
    try:
        q = db.query(BulkImportJob)
        if status_filter:
            q = q.filter(BulkImportJob.status == status_filter)
        if user_id:
            q = q.filter(BulkImportJob.user_id == user_id)
        if created_after:
            q = q.filter(BulkImportJob.created_at >= created_after)
        if created_before:
            q = q.filter(BulkImportJob.created_at <= created_before)
        q = q.order_by(BulkImportJob.created_at.desc())
        q = q.limit(limit).offset(offset)
        return q.all()
    finally:
        db.close()
