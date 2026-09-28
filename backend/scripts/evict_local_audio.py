"""Evict local canonical audio for sessions with a verified Garage copy.

The Garage cutover step. Once a session's durable copy is confirmed present in
Garage (matched by byte size), delete the local working file to reclaim disk.
Garage becomes the home; read paths re-materialize on demand:
  - download/audio  -> services.session_media.resolve_local_path (Garage GET)
  - identify_speakers re-extract fallback -> same resolver

SAFE BY CONSTRUCTION:
  - NEVER deletes a local file without first confirming the Garage object
    exists AND its size matches the local file byte-for-byte (size check via
    list_objects_v2, not HEAD — Garage 400s on HEAD).
  - Idempotent: a session whose local file is already gone is skipped.
  - Reversible: the bytes remain in Garage; resolve_local_path pulls them back
    into the local cache on next access.
  - audio_file column is left intact (resolve falls through to Garage when the
    path no longer exists on disk).
  - Free-tier sessions never have server audio, so they're never touched.

Usage (inside meet-backend):
    python3 scripts/evict_local_audio.py --dry-run
    python3 scripts/evict_local_audio.py                 # evict eligible
    python3 scripts/evict_local_audio.py --keep-days 7   # keep recent local warm
    python3 scripts/evict_local_audio.py --status completed,failed
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("evict")


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def _garage_object_size(client, bucket: str, key: str):
    """Return the object's byte size if it exists in Garage, else None.
    Uses list (not HEAD — Garage 400s on HEAD)."""
    resp = client.list_objects_v2(Bucket=bucket, Prefix=key)
    for o in resp.get("Contents", []):
        if o["Key"] == key:
            return o["Size"]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-days", type=int, default=0,
                    help="skip sessions created within the last N days (keep local warm)")
    ap.add_argument("--status", default="completed",
                    help="comma-list of statuses eligible for eviction (default: completed)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    cutoff = None
    if args.keep_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep_days)

    from database.database import SessionLocal
    from database.models import RecordingSession
    import database.models_rooms  # noqa: F401
    import auth.models  # noqa: F401
    from services import media_storage

    if not media_storage.garage_configured():
        log.error("Garage not configured (or MEDIA_STORAGE_DISABLED) — refusing to evict.")
        return 2
    client = media_storage._client()
    bucket = media_storage.GARAGE_AUDIO_BUCKET

    db = SessionLocal()
    try:
        q = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.audio_object_key.isnot(None),
                RecordingSession.audio_storage_backend == "garage",
                RecordingSession.audio_file.isnot(None),
                RecordingSession.status.in_(statuses),
            )
            .order_by(RecordingSession.id)
        )
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()
        log.info("candidates (garage-backed, status in %s): %d", statuses, len(rows))

        total = evicted = skip_recent = skip_nolocal = skip_unverified = 0
        freed = 0
        for s in rows:
            total += 1
            if cutoff is not None and s.created_at and s.created_at.replace(tzinfo=timezone.utc) > cutoff:
                skip_recent += 1
                continue
            local = Path(s.audio_file)
            if not local.exists():
                skip_nolocal += 1
                continue
            lsize = local.stat().st_size
            gsize = _garage_object_size(client, bucket, s.audio_object_key)
            if gsize is None or gsize != lsize:
                skip_unverified += 1
                log.warning(
                    "skip session=%s — Garage copy unverified (local=%s garage=%s key=%s) — NOT deleting",
                    s.id, lsize, gsize, s.audio_object_key,
                )
                continue
            if args.dry_run:
                evicted += 1
                freed += lsize
                log.info("[dry-run] would evict session=%s %s (%s) — Garage verified", s.id, local.name, _mb(lsize))
                continue
            local.unlink()
            evicted += 1
            freed += lsize
            log.info("evicted session=%s %s (%s) — served from Garage henceforth", s.id, local.name, _mb(lsize))

        log.info(
            "DONE %s — total=%d evicted=%d skip_recent=%d skip_nolocal=%d skip_unverified=%d freed=%s",
            "(dry-run)" if args.dry_run else "(live)",
            total, evicted, skip_recent, skip_nolocal, skip_unverified, _mb(freed),
        )
        return 1 if skip_unverified else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
