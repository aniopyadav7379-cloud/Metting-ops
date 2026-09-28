"""Backfill Project-Ops tasks from existing Meeting-Ops action items.

``projectops_writer`` fires on session completion (reprocess + live), but
action items that predate the bridge — or that were created while PO was
unconfigured — never got a matching PO task. This idempotently reconciles:
it walks completed sessions and calls the writer, which creates a task per
un-stamped action item and skips any row already carrying ``po_task_id``.

Safe + idempotent: the writer never raises, never duplicates (the
``action_items.raw_payload.po_task_id`` stamp is the idempotency key), and
no-ops cleanly when PROJECTOPS_API_KEY is unset. Re-running is harmless.

Usage (inside meet-backend):
    # Wire an org's default target project first (v1 DB-only config path):
    python3 scripts/backfill_action_items_to_projectops.py --set-default-project 1=P-00055

    python3 scripts/backfill_action_items_to_projectops.py --dry-run   # list candidates
    python3 scripts/backfill_action_items_to_projectops.py             # reconcile completed
    python3 scripts/backfill_action_items_to_projectops.py --org-id 1  # one org only
    python3 scripts/backfill_action_items_to_projectops.py --status completed,failed
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("projectops-backfill")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--status",
        default="completed",
        help="comma-list of session statuses (default: completed)",
    )
    ap.add_argument("--org-id", type=int, default=0, help="restrict to one organization")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--set-default-project",
        metavar="ORG_ID=PROJECT_NUMBER",
        default=None,
        help="set Organization.settings.projectops_default_project_number and exit "
        "(e.g. 1=P-00055; pass 'ORG_ID=' to clear)",
    )
    args = ap.parse_args()

    from database.database import SessionLocal
    from database.models import ActionItem, RecordingSession
    import database.models_rooms  # noqa: F401  (register FK targets)
    import auth.models  # noqa: F401
    from services.projectops_writer import (
        set_org_default_project_number,
        write_action_items_to_projectops,
    )

    db = SessionLocal()
    try:
        # --- one-shot config helper: set the org default + exit ---
        if args.set_default_project is not None:
            org_part, _, number = args.set_default_project.partition("=")
            try:
                org_id = int(org_part)
            except ValueError:
                log.error("--set-default-project must be ORG_ID=PROJECT_NUMBER")
                return 2
            ok = set_org_default_project_number(db, org_id, number.strip() or None)
            if not ok:
                log.error("organization id=%s not found", org_id)
                return 1
            log.info(
                "org=%s default project set to %r",
                org_id,
                number.strip() or "(cleared)",
            )
            return 0

        statuses = [s.strip() for s in args.status.split(",") if s.strip()]
        q = db.query(RecordingSession).filter(RecordingSession.status.in_(statuses))
        if args.org_id:
            q = q.filter(RecordingSession.organization_id == args.org_id)
        q = q.order_by(RecordingSession.id)
        if args.limit:
            q = q.limit(args.limit)
        rows = q.all()
        log.info(
            "candidates (status in %s%s): %d sessions",
            statuses,
            f", org={args.org_id}" if args.org_id else "",
            len(rows),
        )

        synced = skipped = noop = no_target = err = 0
        total_created = 0
        for s in rows:
            if args.dry_run:
                n_items = (
                    db.query(ActionItem)
                    .filter(ActionItem.session_id == s.id)
                    .count()
                )
                log.info(
                    "[dry-run] session=%s name=%r action_items=%d",
                    s.id,
                    (s.name or s.title or "")[:60],
                    n_items,
                )
                continue

            res = await write_action_items_to_projectops(
                db=db, session_pk=s.id, completion_mode="reprocess"
            )
            mode = getattr(res, "mode", "?")
            total_created += getattr(res, "created", 0)
            if not getattr(res, "ok", False):
                err += 1
                log.warning(
                    "session=%s FAILED mode=%s detail=%s",
                    s.id,
                    mode,
                    getattr(res, "detail", None),
                )
            elif mode == "no-op":
                noop += 1
            elif mode == "no-target":
                no_target += 1
            else:
                synced += 1
                skipped += getattr(res, "skipped", 0)
                log.info(
                    "session=%s created=%d skipped=%d (%s)",
                    s.id,
                    getattr(res, "created", 0),
                    getattr(res, "skipped", 0),
                    getattr(res, "detail", ""),
                )

        log.info(
            "DONE %s — sessions=%d synced=%d tasks_created=%d items_skipped=%d "
            "no_op=%d no_target=%d failed=%d",
            "(dry-run)" if args.dry_run else "(live)",
            len(rows),
            synced,
            total_created,
            skipped,
            noop,
            no_target,
            err,
        )
        return 1 if err else 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
