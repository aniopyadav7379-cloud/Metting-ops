"""Backfill the Brigade knowledge graph over completed sessions.

The `brigade_writer` fires on session completion, but the live graph
(`agent_meeting_ops_canonical`) was only ever seeded with smoke-test data — the
existing corpus predates the writer / was never reconciled. This idempotently
writes every completed session's Meeting / Speaker / ActionItem / Topic /
Decision nodes (+ edges) so graph-augmented retrieval has a real corpus to
traverse.

Safe + idempotent: `write_meeting_to_brigade` MERGEs on node name and never
raises; re-running is harmless. No-op if Brigade isn't configured. Stamps
`recording_sessions.brigade_synced_at` on success, so the default run only
touches unsynced sessions.

Usage (inside meet-backend):
    python3 scripts/backfill_brigade_graph.py --dry-run   # list what would sync
    python3 scripts/backfill_brigade_graph.py             # sync unsynced completed
    python3 scripts/backfill_brigade_graph.py --all       # re-sync ALL completed
    python3 scripts/backfill_brigade_graph.py --status completed,failed
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("brigade-backfill")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--all", action="store_true", help="re-sync ALL matching sessions, not just unsynced")
    ap.add_argument("--status", default="completed", help="comma-list of statuses (default: completed)")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    from database.database import SessionLocal
    from database.models import RecordingSession
    import database.models_rooms  # noqa: F401  (register FK targets)
    import auth.models  # noqa: F401
    from services.brigade_writer import write_meeting_to_brigade

    statuses = [s.strip() for s in args.status.split(",") if s.strip()]
    db = SessionLocal()
    try:
        q = db.query(RecordingSession).filter(RecordingSession.status.in_(statuses))
        if not args.all:
            q = q.filter(RecordingSession.brigade_synced_at.is_(None))
        q = q.order_by(RecordingSession.id)
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()
        log.info("candidates (status in %s, %s): %d",
                 statuses, "all" if args.all else "unsynced only", len(rows))

        ok = noop = err = 0
        for s in rows:
            if args.dry_run:
                log.info("[dry-run] would sync session=%s name=%r", s.id, (s.name or s.title or "")[:60])
                continue
            res = await write_meeting_to_brigade(s.id, db, completion_mode="reprocess")
            mode = getattr(res, "mode", "?")
            if not getattr(res, "ok", False):
                err += 1
                log.warning("session=%s FAILED mode=%s detail=%s", s.id, mode, getattr(res, "detail", None))
            elif mode == "no-op":
                noop += 1
                log.info("session=%s no-op (Brigade unconfigured?)", s.id)
            else:
                ok += 1
                log.info("session=%s synced (mode=%s)", s.id, mode)

        log.info("DONE %s — total=%d synced=%d no_op=%d failed=%d",
                 "(dry-run)" if args.dry_run else "(live)", len(rows), ok, noop, err)
        return 1 if err else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
