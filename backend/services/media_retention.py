"""Local-disk retention for meeting media — keeps the Garage cutover self-managing.

Garage is the durable canonical store (see `media_storage` / `session_media`).
Local disk is a working cache. This module reclaims local disk WITHOUT ever
touching the durable Garage copy:

  evict_completed_local — delete the local working file of completed sessions
      older than `keep_days`, but ONLY when a size-verified Garage object
      exists. Reversible — the audio re-materializes from Garage on next read.

  cap_media_cache — LRU-evict the re-materialization cache (MEDIA_CACHE_ROOT) to
      a byte budget. Every cache entry is re-fetchable from Garage, so this is
      pure disk reclamation.

`run_retention()` runs both with env-configured budgets; the arq worker calls it
on a daily cron (see workers/bulk_import_worker.py). Safe by construction: never
deletes a Garage object, and never deletes a local file without first confirming
a byte-size-matched durable copy in Garage.

Env:
  MEDIA_RETENTION_ENABLED   (default true)
  MEDIA_RETENTION_KEEP_DAYS (default 7  — keep recent meetings' audio local/warm)
  MEDIA_RETENTION_STATUSES  (default "completed")
  MEDIA_CACHE_MAX_GB        (default 20 — LRU cap on the re-materialization cache)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("MEDIA_RETENTION_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def _keep_days() -> int:
    try:
        return int(os.getenv("MEDIA_RETENTION_KEEP_DAYS", "7"))
    except ValueError:
        return 7


def _statuses() -> list[str]:
    return [s.strip() for s in os.getenv("MEDIA_RETENTION_STATUSES", "completed").split(",") if s.strip()]


def _cache_max_bytes() -> int:
    try:
        gb = float(os.getenv("MEDIA_CACHE_MAX_GB", "20"))
    except ValueError:
        gb = 20.0
    return int(gb * 1024 * 1024 * 1024)


def evict_completed_local(*, keep_days: int | None = None, statuses: list[str] | None = None,
                          dry_run: bool = False) -> dict:
    """Delete local working files for sessions with a size-verified Garage copy,
    older than keep_days. Returns a summary dict. Never raises."""
    from database.database import SessionLocal
    from database.models import RecordingSession
    import database.models_rooms  # noqa: F401  (register FK targets)
    import auth.models  # noqa: F401
    from services import media_storage

    summary = {"evicted": 0, "freed_bytes": 0, "skip_recent": 0,
               "skip_nolocal": 0, "skip_unverified": 0, "candidates": 0}
    if not media_storage.garage_configured():
        logger.info("media_retention: garage not configured — skipping eviction")
        return summary

    keep_days = _keep_days() if keep_days is None else keep_days
    statuses = _statuses() if statuses is None else statuses
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)

    db = SessionLocal()
    try:
        rows = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.audio_object_key.isnot(None),
                RecordingSession.audio_storage_backend == "garage",
                RecordingSession.audio_file.isnot(None),
                RecordingSession.status.in_(statuses),
            )
            .all()
        )
        summary["candidates"] = len(rows)
        for s in rows:
            created = s.created_at.replace(tzinfo=timezone.utc) if (s.created_at and s.created_at.tzinfo is None) else s.created_at
            if created and created > cutoff:
                summary["skip_recent"] += 1
                continue
            local = Path(s.audio_file)
            if not local.exists():
                summary["skip_nolocal"] += 1
                continue
            lsize = local.stat().st_size
            gsize = media_storage.object_size(key=s.audio_object_key)
            if gsize is None or gsize != lsize:
                summary["skip_unverified"] += 1
                logger.warning("media_retention: unverified Garage copy session=%s (local=%s garage=%s) — keeping local",
                               s.id, lsize, gsize)
                continue
            if not dry_run:
                try:
                    local.unlink()
                except OSError as e:
                    logger.warning("media_retention: unlink failed session=%s: %s", s.id, e)
                    continue
            summary["evicted"] += 1
            summary["freed_bytes"] += lsize
    finally:
        db.close()
    logger.info("media_retention.evict_completed_local %s: %s", "(dry-run)" if dry_run else "(live)", summary)
    return summary


def cap_media_cache(*, max_bytes: int | None = None, dry_run: bool = False) -> dict:
    """LRU-evict the re-materialization cache (MEDIA_CACHE_ROOT) down to a byte
    budget. Cache entries are re-fetchable from Garage, so this is safe disk
    reclamation. Returns a summary dict."""
    from services import media_storage

    max_bytes = _cache_max_bytes() if max_bytes is None else max_bytes
    root = media_storage.MEDIA_CACHE_ROOT
    summary = {"cache_bytes_before": 0, "removed": 0, "freed_bytes": 0, "max_bytes": max_bytes}
    if not root.exists():
        return summary

    files: list[tuple[float, int, Path]] = []
    for p in root.rglob("*"):
        if p.is_file() and not p.name.endswith((".dl", ".uploading")):
            try:
                st = p.stat()
                files.append((st.st_atime, st.st_size, p))
            except OSError:
                continue
    total = sum(sz for _, sz, _ in files)
    summary["cache_bytes_before"] = total
    if total <= max_bytes:
        return summary

    # Evict least-recently-accessed first until under budget.
    files.sort(key=lambda t: t[0])
    for _atime, size, path in files:
        if total <= max_bytes:
            break
        if not dry_run:
            try:
                path.unlink()
            except OSError:
                continue
        total -= size
        summary["removed"] += 1
        summary["freed_bytes"] += size
    logger.info("media_retention.cap_media_cache %s: %s", "(dry-run)" if dry_run else "(live)", summary)
    return summary


def run_retention(*, dry_run: bool = False) -> dict:
    """Run both retention passes with env-configured budgets. Called by the arq
    daily cron. Returns a combined summary. Never raises."""
    if not _enabled():
        logger.info("media_retention: disabled (MEDIA_RETENTION_ENABLED=false)")
        return {"enabled": False}
    try:
        evict = evict_completed_local(dry_run=dry_run)
        cache = cap_media_cache(dry_run=dry_run)
        return {"enabled": True, "evict": evict, "cache": cache}
    except Exception as e:  # noqa: BLE001
        logger.exception("media_retention: run_retention failed: %s", e)
        return {"enabled": True, "error": str(e)}
