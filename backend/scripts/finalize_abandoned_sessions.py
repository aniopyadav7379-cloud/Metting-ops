"""Manual entry to the session watchdog. Marks abandoned recording sessions
as failed (see services/session_watchdog.py for the safety contract). The
Arq worker calls the same logic on a 30-min cron; this script is for
on-demand cleanup.

Usage (inside meet-backend):
    python3 scripts/finalize_abandoned_sessions.py --dry-run
    python3 scripts/finalize_abandoned_sessions.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, don't mutate")
    args = ap.parse_args()
    from services.session_watchdog import mark_abandoned_recording_sessions
    res = mark_abandoned_recording_sessions(dry_run=args.dry_run)
    print(json.dumps(res, indent=2))
    return 0 if res.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
