"""Backfill existing session audio into Garage (durable object storage).

One-off, idempotent, additive. For every recording_session that has a local
`audio_file` but no `audio_object_key` yet, push the file to Garage and record
the durable location on the row (audio_storage_backend + audio_object_key).

Safe by construction:
  - Local files are NEVER touched/moved/deleted — Garage is an added copy.
  - Idempotent: rows already carrying audio_object_key are skipped, so the
    script can be re-run after a partial failure.
  - Per-session best-effort: a single failure logs + continues; the row's
    columns stay NULL so the next run retries it.
  - Free-tier sessions are naturally excluded (browser-only → no audio_file).

Usage (inside the meet-backend container):
    python3 scripts/backfill_audio_to_garage.py --dry-run   # report scope
    python3 scripts/backfill_audio_to_garage.py             # do the upload
    python3 scripts/backfill_audio_to_garage.py --limit 5   # bite-size batch
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill")


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, no uploads")
    ap.add_argument("--limit", type=int, default=0, help="cap rows processed (0 = all)")
    args = ap.parse_args()

    from database.database import SessionLocal
    from database.models import RecordingSession
    # Import every model module that owns a table referenced by a
    # RecordingSession FK (conference_rooms, auth orgs/users) so the SQLAlchemy
    # mapper fully configures — otherwise the first expired-attribute reload
    # after a commit blows up on an unresolved FK target.
    import database.models_rooms  # noqa: F401
    import auth.models  # noqa: F401
    from services import media_storage, session_media

    if not media_storage.garage_configured():
        log.error("Garage is not configured (or MEDIA_STORAGE_DISABLED set) — aborting.")
        return 2
    media_storage.ensure_bucket()

    db = SessionLocal()
    try:
        q = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.audio_file.isnot(None),
                RecordingSession.audio_object_key.is_(None),
            )
            .order_by(RecordingSession.id)
        )
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()

        total = uploaded = skipped_missing = failed = 0
        bytes_done = 0
        log.info("candidates (audio_file set, not yet in Garage): %d", len(rows))

        for s in rows:
            total += 1
            local = session_media.resolve_local_path(s)
            if local is None or not local.exists() or local.stat().st_size == 0:
                skipped_missing += 1
                log.warning("skip session=%s — local audio missing (%s)", s.id, s.audio_file)
                continue
            size = local.stat().st_size
            if args.dry_run:
                uploaded += 1
                bytes_done += size
                log.info("[dry-run] would upload session=%s %s (%s)", s.id, local.name, _mb(size))
                continue
            backend = session_media.persist_session_audio(db, s, local_path=str(local))
            if backend == "garage":
                uploaded += 1
                bytes_done += size
                log.info("uploaded session=%s key=%s (%s)", s.id, s.audio_object_key, _mb(size))
            else:
                failed += 1
                log.error("FAILED session=%s (Garage push returned %r)", s.id, backend)

        log.info(
            "DONE %s — total=%d uploaded=%d skipped_missing=%d failed=%d data=%s",
            "(dry-run)" if args.dry_run else "(live)",
            total, uploaded, skipped_missing, failed, _mb(bytes_done),
        )
        return 1 if failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
