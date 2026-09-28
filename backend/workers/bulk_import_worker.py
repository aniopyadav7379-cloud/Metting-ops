"""Arq worker for bulk import processing.

B-import.4: replaces the in-process asyncio queue with a durable Arq
worker backed by Redis. Jobs persist in Redis so a uvicorn restart
mid-batch resumes cleanly.

Config:
  ARQ_ENABLED=true|false  (default: true)
  ARQ_REDIS_URL           (default: redis://:default@unicorn-redis:6379/4)
  BULK_IMPORT_WORKERS     (default: 2, maps to Arq max_jobs)
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

from arq import create_pool, cron
from arq.connections import RedisSettings

from database.database import SessionLocal
from database.models import BulkImportFile, BulkImportJob
# Register the rooms models in this worker's SQLAlchemy metadata at startup so
# any RecordingSession query resolves its room_id -> conference_rooms FK. The
# arq worker imports a narrower module set than the FastAPI app; without this,
# reprocess queries raise NoReferencedTableError (conference_rooms missing).
from database import models_rooms  # noqa: F401
from services.bulk_import_queue import (
    _do_process_file,
    bulk_import_queue,
    get_bulk_import_concurrency,
    start_bulk_import_queue,
    stop_bulk_import_queue,
)

logger = logging.getLogger(__name__)
BATCH_QUEUE_NAME = os.getenv("ARQ_BATCH_QUEUE", "meeting-ops:batch")
INTERACTIVE_QUEUE_NAME = os.getenv("ARQ_INTERACTIVE_QUEUE", "meeting-ops:interactive")


async def worker_startup(ctx: dict[str, Any]) -> None:
    from services.sentry import init_sentry

    init_sentry()


async def process_bulk_file(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Arq task: process one bulk-import file."""
    file_uuid = uuid.UUID(file_id)
    logger.info("arq worker: processing file_id=%s", file_id)
    try:
        await _do_process_file(file_uuid)
        return {"status": "done", "file_id": file_id}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("arq worker: crashed on file_id=%s: %s", file_id, exc)
        await asyncio.to_thread(bulk_import_queue._mark_failed, file_uuid, str(exc))
        return {"status": "failed", "file_id": file_id, "error": str(exc)[:2000]}


async def media_retention_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Daily cron: reclaim local disk now that Garage is the durable home for
    audio. Evicts local working copies of older completed sessions that have a
    size-verified Garage copy, and LRU-caps the re-materialization cache. Never
    deletes a Garage object; fully reversible (audio re-fetches on read)."""
    from services.media_retention import run_retention
    result = await asyncio.to_thread(run_retention)
    logger.info("arq cron: media_retention result=%s", result)
    return result


async def data_retention_purge_task(ctx: dict[str, Any]) -> dict[str, Any]:
    from services.data_retention import run_data_retention_purge
    result = await asyncio.to_thread(run_data_retention_purge)
    logger.info("arq cron: data_retention_purge result=%s", result)
    return result


async def session_watchdog_task(ctx: dict[str, Any]) -> dict[str, Any]:
    """Every-30-min cron: clear stuck sessions + expire comps.

    1. status='recording' with no chunk update past SESSION_WATCHDOG_MINUTES
       (default 30 min) — phone tab evicted, dev tab crashed, etc. Flipped
       to ``failed`` so endpoints iterating active recordings stay fast and
       Cloudflare's 120 s edge timeout doesn't trip (see the 2026-05-29
       524 incident).
    2. status='processing' + needs_summary (a *transient* summary/gateway
       failure left a transcript but no summary, drift-guard wedging any
       retry) — re-driven by ``redrive_stuck_summary_sessions``: drift-guard
       cleared and finalize re-enqueued, bounded to
       SESSION_WATCHDOG_SUMMARY_REDRIVE_MAX attempts (default 5) spaced
       SESSION_WATCHDOG_SUMMARY_REDRIVE_COOLDOWN_MINUTES apart (default 10)
       so a genuinely-broken LLM can't loop forever.
    3. status='processing' with no resolution past
       SESSION_WATCHDOG_PROCESSING_MINUTES (default 360 min = 6 h) — worker
       OOM, host restart mid-pass, Stage 7 exception that didn't bubble up.
       Triaged by content: rows with transcript text → 'completed'
       (preserve the work); rows with no transcript → 'failed'. Added
       2026-06-03 after bigboy accumulated 101 stuck-processing rows from
       a chain of rapid redeploys earlier the same day.

    Audio + transcripts + graph + Garage all stay intact in these passes —
    only status (+ ended_at + processing_metadata audit fields) change.

    A final, independent pass (revert_expired_comps) expires time-limited
    Pro/paid comps: users whose User.tier_expires_at has passed are flipped
    back to 'free' with the expiry cleared, so an admin "free month" grant
    (scripts/grant_pro.py) auto-reverts. Real subscriptions clear
    tier_expires_at (api.stripe_webhook), so paying customers are never
    reverted. Configurable via SESSION_WATCHDOG_* env vars; all default-enabled."""
    from services.session_watchdog import (
        mark_abandoned_uploads,
        mark_abandoned_recording_sessions,
        mark_abandoned_processing_sessions,
        redrive_stuck_summary_sessions,
        revert_expired_comps,
    )
    recording_result = await asyncio.to_thread(mark_abandoned_recording_sessions)
    # Re-drive needs_summary rows BEFORE the processing triage so a transient
    # summary failure is retried (bounded) rather than terminally promoted/failed.
    summary_redrive_result = await asyncio.to_thread(redrive_stuck_summary_sessions)
    processing_result = await asyncio.to_thread(mark_abandoned_processing_sessions)
    upload_result = await asyncio.to_thread(mark_abandoned_uploads)
    # Expire time-limited Pro/paid comps (scripts/grant_pro.py): flip users whose
    # tier_expires_at has passed back to 'free'. Independent of the session
    # passes above; grouped here so one cron owns all watchdog housekeeping.
    comp_revert_result = await asyncio.to_thread(revert_expired_comps)
    combined = {
        "recording": recording_result,
        "summary_redrive": summary_redrive_result,
        "processing": processing_result,
        "uploads": upload_result,
        "comp_revert": comp_revert_result,
    }
    logger.info("arq cron: session_watchdog result=%s", combined)
    return combined


# v3.18.3 background-jobification: long-running handlers now enqueue
# arq jobs instead of awaiting inline (Codex Performance Finding S1).
# The workers themselves live in dedicated modules; we import them here
# so the arq runner registers them on startup.
from workers.finalize_workers import finalize_session_job  # noqa: E402
from workers.reprocess_workers import (  # noqa: E402
    recover_reprocess_jobs,
    reprocess_session_job,
)
from workers.digest_workers import generate_digest_job  # noqa: E402
from workers.weekly_digest_workers import weekly_digest_cron  # noqa: E402
from workers.tts_workers import (  # noqa: E402
    render_tts_summary_job,
    render_tts_podcast_job,
)


class WorkerSettings:
    """Arq worker configuration."""

    functions = [
        process_bulk_file,
        finalize_session_job,
        generate_digest_job,
        weekly_digest_cron,
        render_tts_summary_job,
        render_tts_podcast_job,
        reprocess_session_job,
    ]
    # Daily local-disk retention (Garage cutover housekeeping). 04:30 UTC is a
    # low-traffic window; the task is read-mostly + best-effort.
    # Session watchdog runs every 30 min on the half-hour to keep the active-
    # recordings set tight (see services/session_watchdog.py).
    cron_jobs = [
        cron(media_retention_task, hour={4}, minute={30}),
        cron(data_retention_purge_task, hour={5}, minute={0}),
        cron(session_watchdog_task, minute={0, 30}),
        # Weekly meeting digest email — Monday 13:00 UTC, summarizing the
        # previous Mon-Sun week per opted-in org (see
        # workers/weekly_digest_workers.py). Opt-in via
        # Organization.settings["weekly_digest"]; idempotent via
        # MeetingDigest.emailed_at.
        cron(weekly_digest_cron, weekday="mon", hour={13}, minute={0}),
    ]
    max_jobs = get_bulk_import_concurrency()
    redis_settings = RedisSettings.from_dsn(
        os.getenv("ARQ_REDIS_URL") or os.getenv("REDIS_URL", "redis://unicorn-redis:6379/4")
    )
    job_timeout = 3600
    on_startup = worker_startup
    queue_name = BATCH_QUEUE_NAME


class InteractiveWorkerSettings:
    """Reserved lane for user-facing completion work."""

    functions = [
        finalize_session_job,
        generate_digest_job,
        render_tts_summary_job,
        render_tts_podcast_job,
        reprocess_session_job,
    ]
    max_jobs = max(1, int(os.getenv("ARQ_INTERACTIVE_WORKERS", "2")))
    redis_settings = WorkerSettings.redis_settings
    job_timeout = WorkerSettings.job_timeout
    on_startup = worker_startup
    queue_name = INTERACTIVE_QUEUE_NAME


_arq_pool = None


async def get_arq_pool():
    """Lazily create the Arq Redis pool."""
    global _arq_pool
    if _arq_pool is None:
        _arq_pool = await create_pool(
            RedisSettings.from_dsn(
                os.getenv("ARQ_REDIS_URL") or os.getenv("REDIS_URL", "redis://unicorn-redis:6379/4")
            )
        )
    return _arq_pool


async def close_arq_pool():
    """Close the Arq Redis pool on shutdown."""
    global _arq_pool
    if _arq_pool is not None:
        await _arq_pool.close()
        _arq_pool = None


async def enqueue_file(file_id: uuid.UUID) -> str:
    """Enqueue a single file for processing via Arq."""
    pool = await get_arq_pool()
    j = await pool.enqueue_job(
        "process_bulk_file",
        file_id=str(file_id),
        job_id=f"bulk-import-{file_id}",
        _queue_name=BATCH_QUEUE_NAME,
    )
    return j.job_id


async def recover_pending_jobs():
    """Restart recovery: scan bulk_import_files where status is in
    {queued, uploading} and re-enqueue them."""
    db = SessionLocal()
    try:
        pending = (
            db.query(BulkImportFile)
            .filter(BulkImportFile.status.in_(("queued", "uploading")))
            .all()
        )
        recovered = 0
        for row in pending:
            job = (
                db.query(BulkImportJob)
                .filter(BulkImportJob.id == row.job_id)
                .first()
            )
            if not job or job.cancelled_at is not None or job.status in (
                "cancelled", "complete", "failed",
            ):
                continue
            try:
                await enqueue_file(row.id)
                recovered += 1
                logger.info("arq recovery: re-enqueued file_id=%s", row.id)
            except Exception as exc:
                logger.warning(
                    "arq recovery: failed to enqueue file_id=%s: %s", row.id, exc,
                )
        logger.info(
            "arq recovery: %d/%d pending files re-enqueued",
            recovered, len(pending),
        )
    finally:
        db.close()


async def start_arq_worker():
    """Start the Arq worker pool and recover pending jobs."""
    enabled = os.getenv("ARQ_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        logger.info("arq: disabled (ARQ_ENABLED=false), using in-process queue")
        await start_bulk_import_queue()
        return
    await recover_pending_jobs()
    await recover_reprocess_jobs()
    logger.info("arq worker started: max_jobs=%d, redis_db=4", WorkerSettings.max_jobs)


async def stop_arq_worker():
    """Stop the Arq worker pool."""
    enabled = os.getenv("ARQ_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        await stop_bulk_import_queue()
        return
    await close_arq_pool()
    logger.info("arq worker stopped")


def bulk_import_enqueue(file_id: uuid.UUID) -> bool:
    """Non-blocking enqueue for the API layer."""
    async def _enqueue():
        try:
            await enqueue_file(file_id)
            return True
        except Exception:
            return False
    try:
        loop = asyncio.get_running_loop()
        return loop.run_until_complete(_enqueue())
    except Exception:
        return False
