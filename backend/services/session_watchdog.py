"""Session watchdog — auto-finalize abandoned recording sessions.

Always-on recordings that never get a proper Stop (browser tab closed, user
walked away, devops restart, satellite power-cycle, etc.) sit in the DB at
``status='recording'`` forever. Over time the count creeps up and every
endpoint that filters or iterates "active sessions" amplifies its own work,
which can push slow endpoints past Cloudflare's 120 s edge timeout (see the
zombie pile-up on 2026-05-29: 7 sessions in ``status='recording'`` for
2-9 days, leading to a 524 mid-meeting).

Safe by construction:
  - Only flips sessions whose ``updated_at`` is older than the threshold.
    The default is ``SESSION_WATCHDOG_MINUTES`` (default 30 minutes, v3.22.5),
    superseding the legacy ``SESSION_WATCHDOG_HOURS`` knob (kept for back-compat
    — if both are set, MINUTES wins; if only HOURS is set, we still honor it).
    Real long-running always-on sessions get a new ``updated_at`` every time a
    chunk lands (~every 30 s), so 30 min of zero activity is the right signal
    that a phone tab got evicted or a desktop tab crashed — the original 6 h
    threshold was sized for desktop "leave the laptop running overnight" use
    and didn't catch phone-tab eviction (see 2026-06-01 incident: phone Chrome
    tab evicted at 21 min stale, blocked Record on next visit).
  - Audio is preserved — the Garage object stays, ``audio_object_key`` stays
    on the row. Only ``status`` and ``ended_at`` flip; the watchdog never
    deletes data.
  - Idempotent: re-running finds nothing left to fail.
  - Best-effort and bounded — runs in a single DB session with a hard cap on
    rows touched per pass so a misconfiguration can't cascade.

Triggered by the Arq cron in ``workers/bulk_import_worker.WorkerSettings``;
also exposed as ``scripts/finalize_abandoned_sessions.py`` for manual ops.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)


# Default idle threshold. Phones lose their service worker / MediaRecorder
# the moment the OS swaps the tab out of foreground (often <5 min on iOS).
# 30 minutes is "definitely dead, but won't trip a healthy chunk cadence" —
# the FullAudioRecorder uploads every 30s, so a live session is touched
# 60x within the window.
DEFAULT_WATCHDOG_MINUTES = 30


def _threshold_minutes() -> int:
    """Resolved idle threshold, in minutes.

    Precedence:
      1. ``SESSION_WATCHDOG_MINUTES`` (new in v3.22.5; preferred)
      2. ``SESSION_WATCHDOG_HOURS`` (legacy; multiplied by 60)
      3. :data:`DEFAULT_WATCHDOG_MINUTES`

    Always returns a value >= 1 minute. The MINUTES knob takes priority even
    when HOURS is also set, so a deploy can opt in to the new default by just
    unsetting HOURS (which most prod envs don't set explicitly anyway).
    """
    raw_minutes = os.getenv("SESSION_WATCHDOG_MINUTES")
    if raw_minutes is not None:
        try:
            return max(1, int(raw_minutes))
        except ValueError:
            pass
    raw_hours = os.getenv("SESSION_WATCHDOG_HOURS")
    if raw_hours is not None:
        try:
            return max(1, int(raw_hours) * 60)
        except ValueError:
            pass
    return DEFAULT_WATCHDOG_MINUTES


def _max_per_pass() -> int:
    try:
        return max(1, int(os.getenv("SESSION_WATCHDOG_MAX_PER_PASS", "200")))
    except ValueError:
        return 200


def _enabled() -> bool:
    return os.getenv("SESSION_WATCHDOG_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on",
    )


# Post-processing (Parakeet 1.1B + pyannote 3.1 + summary LLM) typically
# completes in 30-90 s per finished meeting. Anything stuck at
# ``status='processing'`` for hours is a dead pipeline (worker OOM, host
# restart mid-pass, server-side stage 7 exception that didn't bubble up
# to flip the row). 6 hours is the conservative default — comfortably
# past any legitimate large-file batch import and short enough that the
# UI doesn't carry zombies into the next day.
DEFAULT_PROCESSING_WATCHDOG_HOURS = 6
DEFAULT_UPLOAD_WATCHDOG_MINUTES = 120


def _processing_threshold_minutes() -> int:
    raw = os.getenv("SESSION_WATCHDOG_PROCESSING_MINUTES")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_PROCESSING_WATCHDOG_HOURS * 60


def _upload_threshold_minutes() -> int:
    try:
        return max(1, int(os.getenv("UPLOAD_WATCHDOG_MINUTES", str(DEFAULT_UPLOAD_WATCHDOG_MINUTES))))
    except ValueError:
        return DEFAULT_UPLOAD_WATCHDOG_MINUTES


# --- needs_summary re-drive ------------------------------------------------
# A *transient* summarizer/gateway failure leaves a finalized meeting at
# status='processing' + processing_metadata['needs_summary']=True with a
# transcript but no summary (see workers/finalize_workers.finalize_session_job
# step 1). ``redrive_stuck_summary_sessions`` re-kicks finalize for those rows.
# Bounded so a genuinely-broken LLM can't loop forever: at most
# DEFAULT_SUMMARY_REDRIVE_MAX attempts per session, spaced no closer than
# DEFAULT_SUMMARY_REDRIVE_COOLDOWN_MINUTES apart.
DEFAULT_SUMMARY_REDRIVE_COOLDOWN_MINUTES = 10
DEFAULT_SUMMARY_REDRIVE_MAX = 5


def _summary_redrive_cooldown_minutes() -> int:
    raw = os.getenv("SESSION_WATCHDOG_SUMMARY_REDRIVE_COOLDOWN_MINUTES")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_SUMMARY_REDRIVE_COOLDOWN_MINUTES


def _summary_redrive_max_attempts() -> int:
    raw = os.getenv("SESSION_WATCHDOG_SUMMARY_REDRIVE_MAX")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_SUMMARY_REDRIVE_MAX


def mark_abandoned_uploads(*, dry_run: bool = False) -> dict[str, Any]:
    """Mark stale upload pipeline rows failed so they become retryable."""
    if not _enabled():
        return {"enabled": False}

    from database.database import SessionLocal
    from database.models import UploadJob

    threshold_minutes = _upload_threshold_minutes()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)
    stages = ("queued", "extracting", "transcribing", "diarizing", "summarizing", "embedding")
    summary = {
        "enabled": True,
        "kind": "uploads",
        "threshold_minutes": threshold_minutes,
        "marked_failed": 0,
        "errors": 0,
        "dry_run": dry_run,
    }
    db = SessionLocal()
    try:
        jobs = db.query(UploadJob).filter(
            UploadJob.stage.in_(stages),
            UploadJob.updated_at < cutoff,
        ).order_by(UploadJob.id).limit(_max_per_pass()).all()
        now = datetime.now(timezone.utc)
        for job in jobs:
            if not dry_run:
                job.stage = "failed"
                job.error_message = (
                    f"Upload pipeline stalled for more than {threshold_minutes} minutes; retry is available."
                )
                job.job_completed_at = now
            summary["marked_failed"] += 1
        if not dry_run:
            db.commit()
        logger.info("session_watchdog upload pass: %s", summary)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        summary["errors"] += 1
        logger.exception("session_watchdog uploads: top-level error: %s", exc)
    finally:
        db.close()
    return summary


# How long an *empty* always-on session must have existed before the watchdog
# is allowed to HARD-DELETE it (vs the gentler flip-to-``failed``). This is the
# 2026-06-07 fix for the local-then-upload data-loss landmine (lost session db
# 501): under the v3.26.9 model the desktop recorder buffers the whole meeting
# in browser IndexedDB and only POSTs it at Stop, so a *still-recording* (or
# briefly-disconnected) session has NO server audio and NO server transcript —
# it is indistinguishable from an abandoned empty zombie by
# _alwayson_session_is_empty, even though the user's browser is holding the full
# recording and can re-upload it via the orphan banner. Hard-deleting the row
# there 404s that resume-upload and loses the meeting for good. So we only
# DELETE empties old enough that a client-side resume is no longer plausible;
# anything newer flips to ``failed`` (the row survives, so orphan resume still
# resolves to a real session). 24h matches the recorder's own orphan-banner
# "older than 24h" cutoff.
DEFAULT_EMPTY_DELETE_MINUTES = 24 * 60


def _empty_delete_threshold_minutes() -> int:
    raw = os.getenv("SESSION_WATCHDOG_EMPTY_DELETE_MINUTES")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_EMPTY_DELETE_MINUTES


def _empty_alwayson_safe_to_delete(sess: Any, now: datetime) -> bool:
    """True iff an empty always-on zombie is old enough that hard-deleting it
    won't strand a recording the user could still resume-upload from their
    browser.

    Age is measured from ``created_at`` (≈ when recording started), NOT
    ``updated_at`` — under local-then-upload a live session stops touching the
    row the instant it starts buffering locally, so ``updated_at`` says nothing
    about how recoverable the recording is. Missing timestamp → conservative
    ``False`` (never delete what we can't reason about; it flips to ``failed``).
    """
    created = getattr(sess, "created_at", None) or getattr(sess, "started_at", None)
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age_minutes = (now - created).total_seconds() / 60.0
    return age_minutes >= _empty_delete_threshold_minutes()


def _alwayson_session_is_empty(sess: Any) -> bool:
    """True iff ``sess`` is an always-on session that produced NOTHING worth
    keeping: no server-side audio AND no transcript text.

    This is the guard that lets the watchdog *delete* a zombie instead of
    leaving a ``failed`` tombstone in the user's session list. It is
    deliberately strict — every clause must hold:

      - ``mode == 'always_on'``           (never upload/satellite/legacy rows)
      - ``source_type == 'browser_always_on'`` (defense in depth)
      - no audio: both ``audio_file`` (local working copy) and
        ``audio_object_key`` (durable Garage copy) are empty/NULL
      - no transcript: ``transcript_simple`` and ``transcript`` are empty AND
        ``transcript_diarized`` carries zero segments

    Verified against live meet_db (2026-06-03): real always-on meetings keep
    a browser-produced transcript with NO server audio, so an audio-only
    check would wipe real data — the transcript clause is load-bearing.
    """
    if getattr(sess, "mode", None) != "always_on":
        return False
    if getattr(sess, "source_type", None) != "browser_always_on":
        return False
    if (getattr(sess, "audio_file", None) or "").strip():
        return False
    if (getattr(sess, "audio_object_key", None) or "").strip():
        return False
    if (getattr(sess, "transcript_simple", None) or "").strip():
        return False
    if (getattr(sess, "transcript", None) or "").strip():
        return False
    diarized = getattr(sess, "transcript_diarized", None)
    if isinstance(diarized, dict) and diarized.get("segments"):
        return False
    return True


def _hard_delete_alwayson_session(db: Any, sess: Any) -> None:
    """Server-side equivalent of the user clicking Discard on an always-on
    session — the same cascade as ``DELETE /api/recordings/sessions/{id}``
    in ``api/recording.py``:

      1. DB rows: Transcription (keyed on int pk) + AudioFile (keyed on the
         string ``session_id``) + the RecordingSession itself.
      2. Disk: the per-session chunk directory under ``RECORDINGS_DIR/always_on``.
      3. Vector store: Qdrant points for this ``session_id``.

    The DB ``delete()`` calls are staged on the SAME session that the caller
    commits once at the end of the pass (so it stays inside the watchdog's
    single bounded transaction). Disk + Qdrant cleanup is best-effort and
    import-on-use, mirroring the API handler; failures are logged, never
    fatal, because the canonical "gone" signal is the row delete.
    """
    import os as _os
    import shutil as _shutil
    from pathlib import Path as _Path

    from database.models import AudioFile, Transcription
    from auth.models import Organization

    canonical_id = sess.session_id or str(sess.id)
    org_id = sess.organization_id

    # Resolve org slug for the on-disk path. Best-effort: if the org is gone
    # the row delete still proceeds; only disk cleanup is skipped.
    org_slug = None
    try:
        org = db.query(Organization).filter(Organization.id == org_id).first()
        org_slug = getattr(org, "slug", None)
    except Exception:  # noqa: BLE001
        org_slug = None

    # 1. DB rows (staged on the caller's session; committed by the pass).
    db.query(Transcription).filter(Transcription.session_id == sess.id).delete()
    db.query(AudioFile).filter(
        AudioFile.session_id == canonical_id,
        AudioFile.organization_id == org_id,
    ).delete()
    db.delete(sess)

    # 2. Disk cleanup — best-effort. Same layout as api.recording._session_dir:
    #    RECORDINGS_DIR/always_on/<org-slug>/<session-id>.
    if org_slug:
        try:
            recordings_dir = _Path(_os.getenv("RECORDINGS_DIR", "/app/recordings"))
            chunk_dir = recordings_dir / "always_on" / org_slug / canonical_id
            if chunk_dir.exists():
                _shutil.rmtree(chunk_dir, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "session_watchdog.delete: chunk-dir cleanup failed for %s — %s",
                canonical_id, exc,
            )

    # 3. Vector store — best-effort, import-on-use (avoid qdrant on cold path).
    try:
        from services.semantic_search_service import semantic_search

        semantic_search.delete_session(canonical_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "session_watchdog.delete: qdrant cleanup failed for %s — %s",
            canonical_id, exc,
        )


def mark_abandoned_recording_sessions(*, dry_run: bool = False) -> dict[str, Any]:
    """Find sessions stuck in ``status='recording'`` whose ``updated_at`` is
    older than the configured threshold and resolve them.

    Two outcomes per row:
      - Audio-less, transcript-less *always-on* zombies that are also OLD
        (created more than ``SESSION_WATCHDOG_EMPTY_DELETE_MINUTES`` ago, 24h
        default) are HARD-DELETED (rows + disk chunks + Qdrant), so the recorder
        stops accumulating empty ``failed`` tombstones in the session list. See
        :func:`_alwayson_session_is_empty` + :func:`_empty_alwayson_safe_to_delete`
        for the strict guard — the age gate is what keeps a *recent*
        local-then-upload recording (empty server-side but recoverable from the
        browser) from being deleted out from under the orphan banner.
      - Everything else — including recent empty always-on rows — is flipped to
        ``failed`` (audio + transcript + Garage object preserved; only
        ``status`` + ``ended_at`` change), so the row survives for resume.

    Returns a summary dict. Never raises — exceptions are logged and the
    pass terminates cleanly. Idempotent."""
    if not _enabled():
        return {"enabled": False}

    from database.database import SessionLocal
    from database.models import RecordingSession
    # v3.23.3 fix: RecordingSession has a FK to organizations.id. Without
    # the Organization model loaded into SQLAlchemy metadata BEFORE the
    # commit, the worker process raises
    #   NoReferencedTableError: Foreign key associated with column
    #   'recording_sessions.organization_id' could not find table 'organizations'
    # at flush time and the whole pass rolls back — so the watchdog has
    # been firing-but-not-committing for an unknown period (110 zombie
    # sessions accumulated on bigboy by 2026-06-02). Importing the model
    # is enough to register it; we don't need to use the name.
    from auth.models import Organization  # noqa: F401

    threshold_minutes = _threshold_minutes()
    # Legacy field kept for back-compat with any caller / dashboard parsing
    # the old key. New code should read ``threshold_minutes``.
    threshold_hours_legacy = round(threshold_minutes / 60, 3)
    cap = _max_per_pass()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

    summary = {
        "enabled": True,
        "threshold_minutes": threshold_minutes,
        "threshold_hours": threshold_hours_legacy,
        "candidates": 0,
        "marked_failed": 0,
        "deleted": 0,
        "skipped_cap": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    db = SessionLocal()
    try:
        # `updated_at` is bumped by SQLAlchemy `onupdate=` whenever anything
        # writes to the row, including the every-30-s chunk persistence,
        # so this naturally excludes truly-active recordings.
        candidates = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.status == "recording",
                RecordingSession.updated_at < cutoff,
            )
            .order_by(RecordingSession.id)
            .limit(cap + 1)
            .all()
        )
        summary["candidates"] = min(len(candidates), cap)
        if len(candidates) > cap:
            summary["skipped_cap"] = len(candidates) - cap
            candidates = candidates[:cap]

        now = datetime.now(timezone.utc)
        for sess in candidates:
            try:
                # Audio-less + transcript-less always-on zombies: delete the
                # whole thing (rows + disk + Qdrant) instead of leaving a
                # ``failed`` tombstone the user has to look at forever. The
                # guard is strict — only browser_always_on rows with no audio
                # AND no transcript ever reach this branch (see
                # _alwayson_session_is_empty). Real meetings, sessions with
                # any audio/transcript, and upload/satellite rows fall through
                # to the mark-failed path below, byte-for-byte unchanged.
                #
                # AND the row must be OLD enough that no client-side resume is
                # plausible (_empty_alwayson_safe_to_delete). A recent empty is
                # almost certainly a local-then-upload recording mid-flight or
                # briefly offline — its audio lives in the browser and the
                # orphan banner can still re-upload it, so deleting the server
                # row would 404 that resume and lose the meeting (session 501).
                # Recent empties fall through to flip-to-failed (row preserved).
                if _alwayson_session_is_empty(sess) and _empty_alwayson_safe_to_delete(sess, now):
                    if dry_run:
                        summary["deleted"] += 1
                        continue
                    _sid = getattr(sess, "session_id", None)
                    _dbid = getattr(sess, "id", None)
                    _uid = getattr(sess, "user_id", None)
                    _oid = getattr(sess, "organization_id", None)
                    _hard_delete_alwayson_session(db, sess)
                    summary["deleted"] += 1
                    # Same loki-greppable prefix family as force_fail so the
                    # centerdeep dashboard can chart delete-rate alongside it.
                    logger.warning(
                        "session_watchdog.delete_empty session_id=%s db_id=%s "
                        "user_id=%s org_id=%s threshold_minutes=%d",
                        _sid, _dbid, _uid, _oid, threshold_minutes,
                    )
                    continue
                if dry_run:
                    summary["marked_failed"] += 1
                    continue
                sess.status = "failed"
                sess.ended_at = now
                # Compute duration if possible.
                if sess.started_at and not sess.duration:
                    started = sess.started_at
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    try:
                        sess.duration = (now - started).total_seconds()
                    except Exception:
                        pass
                md = dict(sess.processing_metadata or {})
                md["auto_failed_reason"] = (
                    f"abandoned: status=recording with no updates for >"
                    f"{threshold_minutes}m (session_watchdog)"
                )
                md["auto_failed_at"] = now.isoformat()
                sess.processing_metadata = md
                summary["marked_failed"] += 1
                # Structured log line — prod monitoring (loki on centerdeep)
                # greps for this prefix to count force-fail rate. Phone-tab
                # eviction shows up here, so a sudden spike is the canary
                # for "Free mobile users are losing recordings".
                logger.warning(
                    "session_watchdog.force_fail session_id=%s db_id=%s "
                    "user_id=%s org_id=%s idle_minutes=%d threshold_minutes=%d",
                    getattr(sess, "session_id", None),
                    getattr(sess, "id", None),
                    getattr(sess, "user_id", None),
                    getattr(sess, "organization_id", None),
                    threshold_minutes,
                    threshold_minutes,
                )
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning(
                    "session_watchdog: failed to mark session=%s — %s",
                    getattr(sess, "id", None), exc,
                )
        if not dry_run:
            db.commit()
        logger.info("session_watchdog pass: %s", summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.exception("session_watchdog: top-level error: %s", exc)
        summary["errors"] += 1
        try:
            db.rollback()
        except Exception:
            pass
        return summary
    finally:
        db.close()


def mark_abandoned_processing_sessions(*, dry_run: bool = False) -> dict[str, Any]:
    """Sibling to :func:`mark_abandoned_recording_sessions` but for the
    post-processing pipeline.

    Sessions stuck at ``status='processing'`` whose ``created_at`` is older
    than the threshold get triaged by content:

      - has transcript text or ``transcript_diarized`` → flipped to
        ``completed`` so the work surfaces in the UI; the summary stage
        (the most likely culprit) can be re-kicked manually if wanted
      - no transcript content → flipped to ``failed`` so the user sees the
        truth instead of an indefinite spinner

    Same safety posture as the recording watchdog: idempotent, bounded by
    ``SESSION_WATCHDOG_MAX_PER_PASS``, audio + Garage object preserved,
    never raises.

    Threshold is ``SESSION_WATCHDOG_PROCESSING_MINUTES`` (default 360 min =
    6 hours). The shorter recording-side default (30 min) doesn't apply
    here: post-processing has legitimate multi-minute spans for a long
    upload (bulk_import), so the cutoff has to be conservative enough that
    a slow-but-alive pass isn't yanked out from under itself.
    """
    if not _enabled():
        return {"enabled": False}

    from database.database import SessionLocal
    from database.models import RecordingSession
    # Same FK-import gotcha as mark_abandoned_recording_sessions — the
    # commit will fail without these models registered with SQLAlchemy
    # metadata. Imported defensively; we don't reference the names.
    from auth.models import Organization  # noqa: F401
    try:
        from database.models_rooms import RoomAudioSource  # noqa: F401
    except Exception:
        pass

    threshold_minutes = _processing_threshold_minutes()
    cap = _max_per_pass()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold_minutes)

    summary = {
        "enabled": True,
        "kind": "processing",
        "threshold_minutes": threshold_minutes,
        "candidates": 0,
        "promoted_completed": 0,
        "marked_failed": 0,
        "skipped_cap": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    db = SessionLocal()
    try:
        candidates = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.status == "processing",
                RecordingSession.created_at < cutoff,
            )
            .order_by(RecordingSession.id)
            .limit(cap + 1)
            .all()
        )
        summary["candidates"] = min(len(candidates), cap)
        if len(candidates) > cap:
            summary["skipped_cap"] = len(candidates) - cap
            candidates = candidates[:cap]

        now = datetime.now(timezone.utc)
        for sess in candidates:
            try:
                # needs_summary re-drive is owned by
                # redrive_stuck_summary_sessions (bounded + drift-cleared). Here
                # we only resolve the TERMINAL state of a genuinely stuck
                # (older than the processing threshold) row by content: promote
                # if there's a transcript to show, else fail. A row still
                # carrying needs_summary that reaches this age has already
                # exhausted its re-drive budget, so promoting it (transcript
                # visible, summary absent) beats an eternal processing spinner.
                has_text = (
                    bool((sess.transcript or "").strip())
                    or bool((sess.transcript_simple or "").strip())
                    or bool(getattr(sess, "transcript_diarized", None))
                )
                if dry_run:
                    if has_text:
                        summary["promoted_completed"] += 1
                    else:
                        summary["marked_failed"] += 1
                    continue
                if has_text:
                    sess.status = "completed"
                    if not sess.ended_at:
                        sess.ended_at = now
                    summary["promoted_completed"] += 1
                    outcome = "promoted_completed"
                else:
                    sess.status = "failed"
                    summary["marked_failed"] += 1
                    outcome = "marked_failed"
                md = dict(sess.processing_metadata or {})
                md["auto_resolved_reason"] = (
                    f"stuck-processing: created_at>{threshold_minutes}m old "
                    f"(session_watchdog_processing); outcome={outcome}"
                )
                md["auto_resolved_at"] = now.isoformat()
                sess.processing_metadata = md
                logger.warning(
                    "session_watchdog.processing.%s session_id=%s db_id=%s "
                    "user_id=%s org_id=%s age_minutes=%d",
                    outcome,
                    getattr(sess, "session_id", None),
                    getattr(sess, "id", None),
                    getattr(sess, "user_id", None),
                    getattr(sess, "organization_id", None),
                    threshold_minutes,
                )
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning(
                    "session_watchdog.processing: failed to triage session=%s — %s",
                    getattr(sess, "id", None), exc,
                )
        if not dry_run:
            db.commit()
        logger.info("session_watchdog processing pass: %s", summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "session_watchdog.processing: top-level error: %s", exc
        )
        summary["errors"] += 1
        try:
            db.rollback()
        except Exception:
            pass
        return summary
    finally:
        db.close()


def _enqueue_finalize_retry(
    session_pk: int, user_id: int | None, org_id: int | None
) -> str | None:
    """Enqueue ``finalize_session_job`` on the interactive lane via a FRESH,
    short-lived Arq pool bound to THIS call's event loop.

    The watchdog runs sync (the cron calls it through ``asyncio.to_thread`` and
    we spin a brand-new loop here with ``asyncio.run``), so it must NOT reuse
    ``services.job_runner``'s module-global pool — that pool is bound to the arq
    worker's own loop, and driving it from a different loop raises "got Future
    attached to a different loop" (which a per-session try/except would silently
    swallow, so the retry would never fire). A per-call pool sidesteps it.

    Returns the arq job_id, or ``None`` if enqueue produced no job. Isolated as
    a module-level function so it can be monkeypatched in tests without Redis.
    """
    import asyncio

    from arq import create_pool
    from arq.connections import RedisSettings

    async def _run() -> str | None:
        pool = await create_pool(
            RedisSettings.from_dsn(
                os.getenv("ARQ_REDIS_URL")
                or os.getenv("REDIS_URL", "redis://unicorn-redis:6379/4")
            )
        )
        try:
            job = await pool.enqueue_job(
                "finalize_session_job",
                session_pk,
                user_id=user_id,
                org_id=org_id,
                _queue_name=os.getenv(
                    "ARQ_INTERACTIVE_QUEUE", "meeting-ops:interactive"
                ),
            )
            return job.job_id if job else None
        finally:
            await pool.close()

    return asyncio.run(_run())


def redrive_stuck_summary_sessions(*, dry_run: bool = False) -> dict[str, Any]:
    """Re-drive meetings whose summary was silently dropped by a *transient*
    summarizer/gateway failure.

    ``finalize_session_job`` (workers/finalize_workers.py, step 1) marks a
    session ``status='processing'`` + ``processing_metadata['needs_summary']``
    when the summary stage fails, retries in-band up to 5x, then gives up
    best-effort. The row is left with a transcript but NO summary — and its
    ``processing_job_id`` still points at the now-dead finalize job, so the
    worker's drift-guard ("drift on session=N … skipping") turns any naive
    re-enqueue into a no-op. Net effect before this pass: the meeting is wedged
    in ``processing`` forever.

    This pass closes that gap, safely:
      - Selects ``status='processing'`` rows whose ``updated_at`` (the last
        finalize attempt / last re-drive) is older than the cooldown — so a
        just-failed row gets a moment to recover, and a re-driven row is not
        immediately re-driven again.
      - Only touches rows that actually carry ``needs_summary`` AND have a
        transcript worth summarizing (empty transcript → nothing to do).
      - Clears the drift-guard: ``processing_job_id = NULL``, COMMITTED BEFORE
        the enqueue, so the fresh ``finalize_session_job`` (whenever a worker
        picks it up) sees no owner and does not drift-skip — no race against
        this commit landing. Mirrors how a first finalize claims the row from a
        NULL owner in ``api/recording.py``.
      - Bounds re-drives: at most ``_summary_redrive_max_attempts`` per session
        (``processing_metadata['summary_redrive_count']``), spaced by at least
        ``_summary_redrive_cooldown_minutes``. A genuinely-broken LLM therefore
        stops after N attempts instead of re-enqueuing forever; the exhausted
        row is later promoted to ``completed`` (transcript visible) by
        :func:`mark_abandoned_processing_sessions` at its own threshold.

    Idempotent, bounded, and never raises. Safe to run on the watchdog cron.
    """
    if not _enabled():
        return {"enabled": False}

    from database.database import SessionLocal
    from database.models import RecordingSession
    from sqlalchemy.orm.attributes import flag_modified
    # Same FK-import gotcha as the sibling passes — the commit fails at flush
    # time without the referenced models registered on the SQLAlchemy metadata.
    from auth.models import Organization  # noqa: F401
    try:
        from database.models_rooms import RoomAudioSource  # noqa: F401
    except Exception:
        pass

    cooldown_minutes = _summary_redrive_cooldown_minutes()
    max_attempts = _summary_redrive_max_attempts()
    cap = _max_per_pass()
    now = datetime.now(timezone.utc)
    cooldown_cutoff = now - timedelta(minutes=cooldown_minutes)

    summary = {
        "enabled": True,
        "kind": "summary_redrive",
        "cooldown_minutes": cooldown_minutes,
        "max_attempts": max_attempts,
        "candidates": 0,
        "redriven": 0,
        "given_up": 0,
        "skipped_no_transcript": 0,
        "skipped_not_needs_summary": 0,
        "skipped_cap": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    db = SessionLocal()
    try:
        # `updated_at` is the last finalize attempt (finalize commits on summary
        # failure) or the last re-drive (this pass commits + bumps it). The
        # cutoff filter is therefore the per-session cooldown AND keeps hot,
        # actively-processing rows out of the candidate set.
        candidates = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.status == "processing",
                RecordingSession.updated_at < cooldown_cutoff,
            )
            .order_by(RecordingSession.id)
            .limit(cap + 1)
            .all()
        )
        summary["candidates"] = min(len(candidates), cap)
        if len(candidates) > cap:
            summary["skipped_cap"] = len(candidates) - cap
            candidates = candidates[:cap]

        for sess in candidates:
            try:
                metadata = dict(sess.processing_metadata or {})
                if not metadata.get("needs_summary"):
                    # A legitimately long-running processing row (e.g. a big
                    # upload/reprocess) — not ours to touch.
                    summary["skipped_not_needs_summary"] += 1
                    continue

                transcript = (sess.transcript_simple or sess.transcript or "").strip()
                if not transcript:
                    # Nothing to summarize — re-driving would just fail the same
                    # way; leave it for the processing pass to resolve.
                    summary["skipped_no_transcript"] += 1
                    continue

                attempts = int(metadata.get("summary_redrive_count") or 0)
                if attempts >= max_attempts:
                    summary["given_up"] += 1
                    # Stamp + log the give-up exactly once so a broken LLM
                    # doesn't spam the logs every pass.
                    if not metadata.get("summary_redrive_given_up_at") and not dry_run:
                        metadata["summary_redrive_given_up_at"] = now.isoformat()
                        sess.processing_metadata = metadata
                        flag_modified(sess, "processing_metadata")
                        db.commit()
                        logger.warning(
                            "session_watchdog.summary_redrive.give_up session_id=%s "
                            "db_id=%s user_id=%s org_id=%s attempts=%d max=%d",
                            getattr(sess, "session_id", None),
                            getattr(sess, "id", None),
                            getattr(sess, "user_id", None),
                            getattr(sess, "organization_id", None),
                            attempts, max_attempts,
                        )
                    continue

                if dry_run:
                    summary["redriven"] += 1
                    continue

                # 1) Clear the drift-guard + record the attempt, and COMMIT
                #    before enqueue. finalize_session_job's drift check skips
                #    only when processing_job_id is set AND != its own job_id;
                #    NULL means "no owner" so the fresh job always proceeds.
                metadata["summary_redrive_count"] = attempts + 1
                metadata["summary_redrive_at"] = now.isoformat()
                sess.processing_metadata = metadata
                sess.processing_job_id = None
                flag_modified(sess, "processing_metadata")
                sess.updated_at = now
                db.commit()

                # 2) Enqueue the fresh finalize on the interactive lane.
                job_id = _enqueue_finalize_retry(
                    sess.id, sess.user_id, sess.organization_id
                )
                if job_id:
                    metadata["summary_redrive_job_id"] = job_id
                    sess.processing_metadata = metadata
                    flag_modified(sess, "processing_metadata")
                    db.commit()

                summary["redriven"] += 1
                logger.warning(
                    "session_watchdog.summary_redrive session_id=%s db_id=%s "
                    "user_id=%s org_id=%s attempt=%d/%d job_id=%s",
                    getattr(sess, "session_id", None),
                    getattr(sess, "id", None),
                    getattr(sess, "user_id", None),
                    getattr(sess, "organization_id", None),
                    attempts + 1, max_attempts, job_id,
                )
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning(
                    "session_watchdog.summary_redrive: failed to re-drive "
                    "session=%s — %s",
                    getattr(sess, "id", None), exc,
                )
        logger.info("session_watchdog summary_redrive pass: %s", summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "session_watchdog.summary_redrive: top-level error: %s", exc
        )
        summary["errors"] += 1
        try:
            db.rollback()
        except Exception:
            pass
        return summary
    finally:
        db.close()


# --- time-limited Pro/paid comp auto-revert --------------------------------
# An admin can comp a user a bounded Pro/paid tier via scripts/grant_pro.py, and
# an invite-code redemption does the same at signup (auth.invite_codes.
# comp_personal_org_to_pro). BOTH set the comp on TWO surfaces require_feature()
# gates paid server-compute on (billing-1): the USER (tier='pro' +
# User.tier_expires_at = now + N days) AND the user's PERSONAL org (plan='pro').
# This pass is what makes the grant SELF-EXPIRING: once tier_expires_at is in the
# past it flips the user back to 'free', clears the expiry, AND drops the personal
# org plan back to 'free' — so reverting the user doesn't leave the workspace on
# 'pro' (which would still let the reverted user run paid compute in their own
# org). A "free month" comp for an invited cohort therefore can't silently become
# a permanent freebie on either surface. A real Stripe subscription clears
# tier_expires_at (api.stripe_webhook), so paying customers and permanent
# (NULL-expiry) grants are never touched. Superusers (support/dev, who resolve to
# enterprise via auth.tier.get_user_tier) are excluded so a stray comp on an admin
# account is a no-op.


def revert_expired_comps(*, dry_run: bool = False) -> dict[str, Any]:
    """Revert users whose time-limited tier comp has expired back to 'free'.

    Selects users where ``tier != 'free'`` AND ``tier_expires_at IS NOT NULL``
    AND ``tier_expires_at < now()`` (excluding superusers), sets ``tier='free'``
    and clears ``tier_expires_at``. A NULL ``tier_expires_at`` means a permanent
    tier and is NEVER touched — so real subscriptions (which clear the column;
    see api.stripe_webhook) and manually-set permanent tiers are safe.

    billing-1: because a comp set BOTH the user tier AND the user's personal org
    plan (scripts/grant_pro.py + auth.invite_codes.comp_personal_org_to_pro),
    this ALSO drops that personal org's plan back to 'free' on revert. Reverting
    only the user would leave the workspace on 'pro', so the per-workspace gate
    (require_feature) would still let the reverted user run paid compute in their
    own org. The org is resolved with auth.invite_codes._resolve_personal_org
    (the same helper the grant + comp use, so grant/revert target the same org),
    and the org revert is best-effort + idempotent (skipped when already 'free')
    and wrapped so it can never break the user revert.

    Founding flags are intentionally left untouched: cohort membership is a
    separate lifecycle from the current paid grant, and the auto-revert's remit
    is only the tier/expiry pair + the paired org plan (grant_pro.py owns founding).

    Bounded by ``SESSION_WATCHDOG_MAX_PER_PASS``, idempotent (a reverted user is
    'free' with a NULL expiry + a 'free' personal org, so a second pass finds
    nothing), and never raises. Safe on the watchdog cron."""
    if not _enabled():
        return {"enabled": False}

    from database.database import SessionLocal
    from auth.models import User
    # Same resolver the grant + invite comp use, so the org that got comped is
    # the org that gets reverted (symmetric). Import-on-use to keep the cold path
    # light + avoid any import-order coupling.
    from auth.invite_codes import _resolve_personal_org

    cap = _max_per_pass()
    now = datetime.now(timezone.utc)

    summary = {
        "enabled": True,
        "kind": "comp_revert",
        "candidates": 0,
        "reverted": 0,
        "orgs_reverted": 0,
        "skipped_cap": 0,
        "errors": 0,
        "dry_run": dry_run,
    }

    db = SessionLocal()
    try:
        candidates = (
            db.query(User)
            .filter(
                User.tier != "free",
                User.tier_expires_at.isnot(None),
                User.tier_expires_at < now,
                # A superuser resolves to enterprise regardless of the tier
                # column (auth.tier.get_user_tier), so never revert one — a comp
                # on an admin is meaningless and reverting it is pointless churn.
                User.is_superuser.isnot(True),
            )
            .order_by(User.id)
            .limit(cap + 1)
            .all()
        )
        summary["candidates"] = min(len(candidates), cap)
        if len(candidates) > cap:
            summary["skipped_cap"] = len(candidates) - cap
            candidates = candidates[:cap]

        for user in candidates:
            try:
                prev_tier = user.tier
                expired_at = user.tier_expires_at
                if dry_run:
                    summary["reverted"] += 1
                    continue
                user.tier = "free"
                user.tier_expires_at = None
                summary["reverted"] += 1
                # billing-1: also drop the user's personal org back to 'free' so
                # the per-workspace gate matches the user revert. Best-effort +
                # idempotent (skip when already free); a failure here must never
                # abort the user revert, so it's wrapped independently. Staged on
                # the same session and flushed by the single commit at pass end.
                org_reverted = False
                try:
                    org = _resolve_personal_org(db, user)
                    if org is not None and (org.plan or "free") != "free":
                        org.plan = "free"
                        db.add(org)
                        summary["orgs_reverted"] += 1
                        org_reverted = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "session_watchdog.comp_revert: personal-org revert "
                        "failed for user=%s — %s (user tier still reverted)",
                        getattr(user, "id", None), exc,
                    )
                # Structured, loki-greppable prefix so the centerdeep dashboard
                # can chart comp-expiry rate alongside the other watchdog passes.
                logger.warning(
                    "session_watchdog.comp_revert user_id=%s email=%s "
                    "tier=%s->free expired_at=%s founding=%s org_reverted=%s",
                    getattr(user, "id", None),
                    getattr(user, "email", None),
                    prev_tier,
                    expired_at.isoformat() if expired_at else None,
                    getattr(user, "is_founding_member", None),
                    org_reverted,
                )
            except Exception as exc:  # noqa: BLE001
                summary["errors"] += 1
                logger.warning(
                    "session_watchdog.comp_revert: failed for user=%s — %s",
                    getattr(user, "id", None), exc,
                )
        if not dry_run:
            db.commit()
        logger.info("session_watchdog comp_revert pass: %s", summary)
        return summary
    except Exception as exc:  # noqa: BLE001
        logger.exception("session_watchdog.comp_revert: top-level error: %s", exc)
        summary["errors"] += 1
        try:
            db.rollback()
        except Exception:
            pass
        return summary
    finally:
        db.close()
