#!/usr/bin/env python3
"""Backfill recording_sessions.participants[].contact_id from an
email -> contact_id mapping.

Population path for the inbound Customer-Ops federation reads: a meeting
"belongs to" a contact when that contact_id is stamped on a participant
entry. New participants can carry contact_id directly (the participants
API accepts it); this script backfills HISTORICAL meetings.

It takes an explicit mapping so it makes ZERO assumptions about any
sibling app's API shape — Contact-Ops / Customer-Ops produce the
email->contact_id mapping (they own resolution) and hand it here:

    {"alice@acme.com": "019e...-person-uuid", "bob@acme.com": "019e...."}

Usage (inside the backend container / venv):

    python -m scripts.backfill_participant_contact_ids \
        --org-id 3 --mapping /tmp/email_to_contact.json [--dry-run]

Matching is case-insensitive on email. Only participant entries that
have an email present in the mapping and DON'T already carry the same
contact_id are touched. Idempotent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from sqlalchemy.orm.attributes import flag_modified

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_contact_ids")


def _load_mapping(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise SystemExit("mapping file must be a JSON object {email: contact_id}")
    return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if k and v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--org-id", type=int, required=True)
    ap.add_argument("--mapping", required=True, help="JSON {email: contact_id}")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mapping = _load_mapping(args.mapping)
    if not mapping:
        log.info("empty mapping — nothing to do")
        return 0

    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    touched_sessions = 0
    touched_participants = 0
    try:
        rows = (
            db.query(RecordingSession)
            .filter(RecordingSession.organization_id == args.org_id)
            .all()
        )
        for s in rows:
            parts = s.participants if isinstance(s.participants, list) else []
            changed = False
            for p in parts:
                if not isinstance(p, dict):
                    continue
                email = (p.get("email") or "").strip().lower()
                if not email:
                    continue
                cid = mapping.get(email)
                if cid and p.get("contact_id") != cid:
                    p["contact_id"] = cid
                    changed = True
                    touched_participants += 1
            if changed:
                s.participants = parts
                flag_modified(s, "participants")
                touched_sessions += 1
        if args.dry_run:
            db.rollback()
            log.info(
                "[dry-run] would stamp %d participants across %d sessions (org %d)",
                touched_participants,
                touched_sessions,
                args.org_id,
            )
        else:
            db.commit()
            log.info(
                "stamped %d participants across %d sessions (org %d)",
                touched_participants,
                touched_sessions,
                args.org_id,
            )
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
