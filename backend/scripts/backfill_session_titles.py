"""Backfill real titles onto sessions still wearing the auto-generated
"Always-on YYYY-MM-DD HH:MM" default.

Background: always-on sessions get a placeholder title at start
(api/recording.py: f"Always-on {started_at:%Y-%m-%d %H:%M}"). A real title is
only assigned when `_summarize_session` successfully summarizes the
transcript. Sessions whose transcript was too short to summarize — or whose
summarizer LLM failed — keep the placeholder forever, so the dashboard fills
with ~dozens of identical "Always-on ..." rows. The finalize worker now
titles these going forward (workers/finalize_workers.py Step 1b); this script
fixes the existing backlog using the SAME titler.

Safe by construction:
  - DRY-RUN BY DEFAULT. Nothing is written unless you pass --commit.
  - Only touches rows whose title is still a default placeholder AND whose
    user never set a title (title_user_set is False) AND that have a
    non-empty transcript. Real / user-set titles are never overwritten.
  - Org-aware: process all orgs (default) or a single org via --org.
  - Per-session best-effort: a single titling failure logs + continues; that
    row keeps its default and the next run retries it. Idempotent — re-running
    only re-touches rows that are still default.
  - The titler routes through each session's own org ProviderRegistry "fast"
    task, so per-org provider/billing config is honored.

Usage (inside the meet-backend container):
    python3 scripts/backfill_session_titles.py                 # dry-run report
    python3 scripts/backfill_session_titles.py --limit 5       # dry-run, 5 rows
    python3 scripts/backfill_session_titles.py --org 1         # dry-run, org 1 only
    python3 scripts/backfill_session_titles.py --commit        # actually write
    python3 scripts/backfill_session_titles.py --commit --limit 20 --org 1
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("backfill_titles")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--commit",
        action="store_true",
        help="actually write titles. Without this flag the script is a "
        "DRY RUN: it reports what it would do and writes nothing.",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="cap number of sessions processed (0 = all).",
    )
    ap.add_argument(
        "--org",
        type=int,
        default=None,
        help="restrict to a single organization_id (default: all orgs).",
    )
    args = ap.parse_args()

    dry_run = not args.commit

    from sqlalchemy import and_, or_

    from database.database import SessionLocal
    from database.models import RecordingSession
    # Import every model module that owns a table referenced by a
    # RecordingSession FK (rooms, auth orgs/users) so the SQLAlchemy mapper
    # fully configures — otherwise the first expired-attribute reload after a
    # commit blows up on an unresolved FK target. Matches
    # backfill_audio_to_garage.py.
    import database.models_rooms  # noqa: F401
    import auth.models  # noqa: F401

    from services.unified_agent_service import (
        generate_title_from_transcript_sync,
        is_default_session_title,
    )

    db = SessionLocal()
    try:
        # Pre-filter at the DB level to the obvious candidates: not user-set,
        # default-looking title, some transcript text present. is_default_…
        # re-checks each row precisely in Python (covers empty / placeholder
        # titles the LIKE can't express). The LIKE 'Always-on %' is the hot
        # path that matches the live backlog.
        has_transcript = or_(
            and_(
                RecordingSession.transcript_simple.isnot(None),
                RecordingSession.transcript_simple != "",
            ),
            and_(
                RecordingSession.transcript.isnot(None),
                RecordingSession.transcript != "",
            ),
        )
        q = db.query(RecordingSession).filter(
            RecordingSession.title_user_set.is_(False),
            or_(
                RecordingSession.title.like("Always-on %"),
                RecordingSession.title.is_(None),
                RecordingSession.title == "",
            ),
            has_transcript,
        )
        if args.org is not None:
            q = q.filter(RecordingSession.organization_id == args.org)
        q = q.order_by(RecordingSession.id)
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()

        log.info(
            "candidates (default title, not user-set, has transcript%s): %d",
            f", org={args.org}" if args.org is not None else "",
            len(rows),
        )
        if dry_run:
            log.info("DRY RUN — no titles will be written. Pass --commit to apply.")

        total = retitled = skipped = failed = 0
        for s in rows:
            total += 1
            # Precise re-check (covers placeholder titles beyond the LIKE).
            if getattr(s, "title_user_set", False) or not is_default_session_title(
                getattr(s, "title", None)
            ):
                skipped += 1
                log.info("skip session=%s — title not a default placeholder (%r)", s.id, s.title)
                continue
            transcript = (s.transcript_simple or s.transcript or "").strip()
            if not transcript:
                skipped += 1
                log.info("skip session=%s — empty transcript", s.id)
                continue
            if s.organization_id is None:
                skipped += 1
                log.warning("skip session=%s — no organization_id (cannot route LLM)", s.id)
                continue

            if dry_run:
                # Don't spend LLM calls on a dry run — just report scope.
                retitled += 1
                log.info(
                    "[dry-run] would retitle session=%s org=%s (transcript %d chars) — current=%r",
                    s.id, s.organization_id, len(transcript), s.title,
                )
                continue

            try:
                generated = generate_title_from_transcript_sync(
                    s.organization_id, transcript
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.error("FAILED session=%s — titler raised: %s", s.id, exc)
                continue

            if not generated:
                failed += 1
                log.warning(
                    "FAILED session=%s — titler returned nothing (LLM down / unusable)", s.id
                )
                continue

            old = s.title
            s.title = generated
            if not (s.name or "").strip() or is_default_session_title(getattr(s, "name", None)):
                s.name = generated
            try:
                db.commit()
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                failed += 1
                log.error("FAILED session=%s — commit error: %s", s.id, exc)
                continue
            retitled += 1
            log.info("retitled session=%s org=%s %r -> %r", s.id, s.organization_id, old, generated)

        log.info(
            "DONE %s — total=%d %s=%d skipped=%d failed=%d",
            "(dry-run)" if dry_run else "(commit)",
            total,
            "would_retitle" if dry_run else "retitled",
            retitled,
            skipped,
            failed,
        )
        return 1 if failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
