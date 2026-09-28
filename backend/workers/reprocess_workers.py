"""Arq worker: bounded, fair server-side session reprocess — tier-A launch.

Moves `_run_session_reprocess` (api/recording.py — the whole-file transcribe +
diarize + summary + title pass fired at Stop via /full-audio) off unbounded
fire-and-forget BackgroundTasks and onto the existing Arq/Redis worker (shared
with bulk-import, redis db 4), so:

  - GPU concurrency is bounded by the worker's ``max_jobs`` — no single account
    can monopolize the pipeline; everyone queues.
  - a per-org daily audio-hours SOFT cap DEPRIORITIZES (never blocks): once an
    org passes ``REPROCESS_MAX_DAILY_AUDIO_HOURS_PER_ORG`` (default 16) of audio
    submitted today, its new jobs are deferred to the back of the line so other
    orgs run first — then they still complete.

Only paid tiers reach here (``canonical_reprocess`` gates Free/Basic out
upstream in api/recording.py before enqueue).

``REPROCESS_ARQ_ENABLED=false`` (or ``ARQ_ENABLED=false``) keeps the legacy
in-process path — a direct ``_run_session_reprocess`` call — which preserves the
test suite's monkeypatches of ``recording._run_session_reprocess`` and supports
any deploy that hasn't flipped the flag. Mirrors workers/bulk_import_worker.py.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Compare-and-delete: only release the in-flight lock if WE still hold it
# (the value matches our token). Prevents a finishing job from clearing a
# DIFFERENT job's freshly-acquired lock in the tiny window between run-complete
# and the finally below.
_RELEASE_LOCK_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _arq_enabled() -> bool:
    raw = os.getenv("REPROCESS_ARQ_ENABLED")
    if raw is None:
        raw = os.getenv("ARQ_ENABLED", "true")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _max_daily_audio_hours() -> float:
    try:
        return max(0.0, float(os.getenv("REPROCESS_MAX_DAILY_AUDIO_HOURS_PER_ORG", "16")))
    except ValueError:
        return 16.0


def _over_cap_defer_seconds() -> int:
    try:
        return max(0, int(os.getenv("REPROCESS_OVER_CAP_DEFER_SECONDS", "300")))
    except ValueError:
        return 300


def _reprocess_lock_ttl() -> int:
    """TTL (seconds) on the per-session in-flight lock. Must comfortably exceed
    (soft-cap defer + worst-case reprocess runtime) so a live job never loses
    its own lock, while still self-freeing if the worker dies mid-job. Default
    2h — a 3h meeting reprocesses in a few minutes, so this is generous."""
    try:
        return max(60, int(os.getenv("REPROCESS_LOCK_TTL_SECONDS", "7200")))
    except ValueError:
        return 7200


def _reprocess_lock_key(session_pk: int) -> str:
    return f"reprocess:lock:{session_pk}"


# --------------------------------------------------------------------------- #
# Daily per-org audio-hours counter (Redis, self-expiring at UTC midnight)
# --------------------------------------------------------------------------- #
def _today_key(org_id: int) -> str:
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"reprocess:org:{org_id}:audio_hours:{day}"


def _seconds_until_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((midnight - now).total_seconds()))


async def _add_org_audio_hours(redis, org_id: int, hours: float) -> float:
    """Atomically add ``hours`` to the org's today counter (INCRBYFLOAT) + set a
    midnight-UTC TTL so it self-resets daily; return the new daily total.

    Fail-open: any redis error returns 0.0 so a metering hiccup never blocks or
    wrongly defers a reprocess."""
    try:
        key = _today_key(org_id)
        new_total = await redis.incrbyfloat(key, float(hours))
        try:
            await redis.expire(key, _seconds_until_utc_midnight())
        except Exception:  # noqa: BLE001
            pass
        return float(new_total)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reprocess metering incr failed org=%s: %s", org_id, exc)
        return 0.0


# --------------------------------------------------------------------------- #
# Org / duration resolution (for metering)
# --------------------------------------------------------------------------- #
def _resolve_org_and_hours(session_pk: int) -> tuple[Optional[int], float]:
    """Look up (organization_id, audio_hours) for metering. Sync DB read; the
    enqueue path runs at Stop, not a hot loop. Best-effort -> (None, 0.0).

    Imports Organization + RoomAudioSource so a RecordingSession query doesn't
    raise NoReferencedTableError (the FK-registration gotcha the watchdog also
    guards against)."""
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession
        from auth.models import Organization  # noqa: F401
        try:
            from database.models_rooms import RoomAudioSource  # noqa: F401
        except Exception:
            pass
        db = SessionLocal()
        try:
            s = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
            if not s:
                return None, 0.0
            org_id = getattr(s, "organization_id", None)
            dur = getattr(s, "duration", None)
            hours = (float(dur) / 3600.0) if dur else 0.0
            return org_id, hours
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("reprocess org/hours resolve failed session=%s: %s", session_pk, exc)
        return None, 0.0


# --------------------------------------------------------------------------- #
# The Arq job — wraps _run_session_reprocess unchanged
# --------------------------------------------------------------------------- #
async def reprocess_session_job(
    ctx: dict[str, Any],
    session_pk: int,
    org_id: Optional[int] = None,
    audio_hours: float = 0.0,
    lock_token: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run the whole-file reprocess for a session inside the bounded worker.

    Thin wrapper: imports ``_run_session_reprocess`` at call time (so a test
    monkeypatch is honored), runs it, returns a status dict. The wrapped
    function owns its own status/metadata writes (incl. the v3.26.13 status
    flip).

    Releases the per-session in-flight lock in a ``finally`` so a later
    reprocess of the same session isn't blocked. Compare-and-delete by
    ``lock_token`` so we only clear OUR lock. ``lock_token=None`` (e.g. a job
    enqueued by pre-lock code still in the queue across a deploy) is a no-op."""
    from middleware.request_context import bind_request_id
    bind_request_id(request_id or ctx.get("job_id"))
    try:
        from api.recording import _run_session_reprocess
        await _run_session_reprocess(session_pk)
        return {"status": "completed", "session_pk": session_pk, "org_id": org_id}
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("reprocess_session_job crash session=%s", session_pk)
        from services.transient_errors import is_transient_error, retry_transient
        if is_transient_error(exc):
            await retry_transient(ctx, exc)
        return {"status": "failed", "session_pk": session_pk, "error": str(exc)[:500]}
    finally:
        if lock_token:
            try:
                redis = ctx.get("redis")
                if redis is not None:
                    await redis.eval(
                        _RELEASE_LOCK_LUA, 1, _reprocess_lock_key(session_pk), lock_token
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reprocess lock release failed session=%s (TTL will reclaim): %s",
                    session_pk, exc,
                )


# --------------------------------------------------------------------------- #
# Enqueue helper — the single entry point all call sites use
# --------------------------------------------------------------------------- #
def _inprocess(session_pk: int, background_tasks) -> None:
    from api.recording import _run_session_reprocess
    if background_tasks is not None:
        background_tasks.add_task(_run_session_reprocess, session_pk)
    else:
        asyncio.create_task(_run_session_reprocess(session_pk))


async def enqueue_reprocess(
    session_pk: int, *, background_tasks=None, force: bool = False
) -> Optional[str]:
    """Enqueue a reprocess.

    ARQ on  -> bounded Arq job + per-org daily soft-cap deprioritization,
               gated by a per-session in-flight lock so only ONE reprocess per
               session runs at a time. A duplicate enqueue while one is in
               flight is a NO-OP (returns None — the running job finishes the
               work); it is NOT an in-process fallback.
    ARQ off -> legacy in-process direct call (preserves test monkeypatches +
               ARQ-disabled deploys).
    Any Arq error falls back to in-process so a reprocess is never lost.

    ``force=True`` (recover_reprocess_jobs / an explicit re-run) clears any
    stale lock first so a leftover from a crashed worker can't block recovery.

    Lock design (supersedes the old stable ``_job_id`` dedup): the job_id is
    now UNIQUE per attempt, so Arq's keep_result window can never silently
    swallow a legitimate later reprocess of the same session — the concurrency
    dedup is the lock, and the lock self-frees via TTL if a worker dies."""
    if not _arq_enabled():
        _inprocess(session_pk, background_tasks)
        return None

    lock_key = _reprocess_lock_key(session_pk)
    lock_token = uuid.uuid4().hex
    try:
        from workers.bulk_import_worker import get_arq_pool
        pool = await get_arq_pool()

        if force:
            # Single reprocess worker => on startup the prior lock holder is
            # gone, so clearing a leftover lock is safe (the TTL would reclaim
            # it eventually anyway).
            try:
                await pool.delete(lock_key)
            except Exception:  # noqa: BLE001
                pass

        acquired = await pool.set(lock_key, lock_token, nx=True, ex=_reprocess_lock_ttl())
        if not acquired:
            logger.info(
                "reprocess already in-flight session=%s — skipping duplicate enqueue",
                session_pk,
            )
            return None

        try:
            org_id, audio_hours = _resolve_org_and_hours(session_pk)
            defer = 0
            if org_id is not None and audio_hours > 0:
                new_total = await _add_org_audio_hours(pool, org_id, audio_hours)
                if new_total > _max_daily_audio_hours():
                    defer = _over_cap_defer_seconds()
                    logger.warning(
                        "reprocess soft-cap: org=%s at %.1fh/day (cap %.0f) — deferring %ss "
                        "(deprioritize, not block)",
                        org_id, new_total, _max_daily_audio_hours(), defer,
                    )
            # Arq reserved kwargs are underscore-prefixed (_job_id, _defer_by).
            # lock_token rides along as a positional arg so the job can
            # compare-and-delete only its OWN lock on finish.
            kwargs: dict[str, Any] = {
                "_job_id": f"reprocess-{session_pk}-{lock_token[:8]}",
                "_queue_name": os.getenv("ARQ_INTERACTIVE_QUEUE", "meeting-ops:interactive"),
            }
            if defer:
                kwargs["_defer_by"] = defer
            job = await pool.enqueue_job(
                "reprocess_session_job", session_pk, org_id, audio_hours, lock_token, **kwargs
            )
            return job.job_id if job else None
        except Exception:
            # Enqueue failed AFTER we took the lock — release it (compare-and-
            # delete by our token) so the session isn't blocked, then re-raise
            # into the in-process fallback below.
            try:
                await pool.eval(_RELEASE_LOCK_LUA, 1, lock_key, lock_token)
            except Exception:  # noqa: BLE001
                pass
            raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reprocess enqueue failed session=%s (%s) — in-process fallback",
            session_pk, exc,
        )
        _inprocess(session_pk, background_tasks)
        return None


# --------------------------------------------------------------------------- #
# Restart recovery — re-enqueue reprocesses in flight at crash
# --------------------------------------------------------------------------- #
async def recover_reprocess_jobs() -> None:
    """On worker start, re-enqueue sessions stuck mid-reprocess (status=processing,
    non-terminal reprocess_status, updated within 6h) so a worker restart doesn't
    strand them until the watchdog. Bounded + best-effort."""
    if not _arq_enabled():
        return
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession
        from auth.models import Organization  # noqa: F401
        try:
            from database.models_rooms import RoomAudioSource  # noqa: F401
        except Exception:
            pass
        cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
        db = SessionLocal()
        try:
            rows = (
                db.query(RecordingSession)
                .filter(
                    RecordingSession.status == "processing",
                    RecordingSession.updated_at >= cutoff,
                )
                .order_by(RecordingSession.id.desc())
                .limit(200)
                .all()
            )
            recovered = 0
            for s in rows:
                md = s.processing_metadata or {}
                if md.get("reprocess_status") == "complete":
                    continue
                try:
                    # force=True clears the stale in-flight lock the crashed
                    # worker left behind so the re-enqueue isn't a no-op.
                    await enqueue_reprocess(s.id, force=True)
                    recovered += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("reprocess recovery: re-enqueue failed session=%s: %s", s.id, exc)
            logger.info("reprocess recovery: %d processing sessions re-enqueued", recovered)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("reprocess recovery failed: %s", exc)
