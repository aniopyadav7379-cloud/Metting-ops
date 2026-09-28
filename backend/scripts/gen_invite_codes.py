#!/usr/bin/env python3
"""Generate single-use beta invite codes (Pro comp on redemption).

Each code, when redeemed at ``POST /api/auth/register``, comps the new user to
a TIME-LIMITED Pro tier on BOTH surfaces require_feature() gates paid
server-compute on (billing-1): ``user.tier='pro'`` + ``tier_expires_at`` AND
their PERSONAL org ``plan='pro'`` — see
``auth.invite_codes.comp_personal_org_to_pro``. The session-watchdog auto-revert
(``services.session_watchdog.revert_expired_comps``) expires the comp on both
surfaces once ``tier_expires_at`` passes.

This is the node-side / ops equivalent of the admin mint endpoint
(``POST /api/admin/invite-codes``): use it when you have a DB DSN but not an
authenticated superuser session (e.g. a one-off cohort seed on bigboy).

IMPORTANT — comp duration is NOT stored per-code. ``BetaInviteCode`` has no
duration column (schema: code / max_redemptions / redemption_count /
redeemed_by_user_id / redeemed_at / is_active / note / created_by_user_id /
created_at). So every redemption applies the redemption-time default,
``auth.invite_codes.DEFAULT_COMP_DAYS`` (30 days). ``--days`` here is recorded
in each code's ``note`` for cohort tracking and documents the INTENDED length;
it does NOT change the enforced length. To actually change the enforced length,
change ``DEFAULT_COMP_DAYS`` (a global change for all invite comps) or add a
per-code column in a new migration. If ``--days`` diverges from the enforced
default the script prints a loud warning so the divergence is explicit.

Usage (inside meet-backend, or from backend/ with DATABASE_URL set):
    python -m scripts.gen_invite_codes --count 25
    python -m scripts.gen_invite_codes --count 25 --days 30 --cohort meeting_ops_v1
    python -m scripts.gen_invite_codes --count 10 --note "wave 2" --for-user-id 4

Prints one code per line to stdout (pipe-friendly:
``python -m scripts.gen_invite_codes --count 25 > codes.txt``); a human summary
+ any warnings go to stderr.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate single-use beta invite codes (Pro comp on redemption).",
    )
    ap.add_argument("--count", type=int, required=True,
                    help="number of codes to generate (>= 1)")
    ap.add_argument("--days", type=int, default=None,
                    help="INTENDED comp length in days (default: the enforced "
                         "DEFAULT_COMP_DAYS). Recorded in each code's note only; "
                         "NOT stored per-code (see module docstring).")
    ap.add_argument("--cohort", default="meeting_ops_v1",
                    help="cohort label recorded in each code's note (default meeting_ops_v1)")
    ap.add_argument("--note", default=None,
                    help="extra free-text note appended to each code's note")
    ap.add_argument("--max-redemptions", type=int, default=1,
                    help="redemptions per code (default 1 = single-use)")
    ap.add_argument("--for-user-id", type=int, default=None,
                    help="attribute the codes to this user id (they appear in that "
                         "user's GET /api/invite-codes/mine); default: unattributed")
    args = ap.parse_args()

    if args.count < 1:
        print("ERROR: --count must be >= 1", file=sys.stderr)
        return 2
    if args.max_redemptions < 1:
        print("ERROR: --max-redemptions must be >= 1", file=sys.stderr)
        return 2

    from auth.invite_codes import DEFAULT_COMP_DAYS, generate_code
    from auth.models import BetaInviteCode, User
    from database.database import SessionLocal

    intended_days = args.days if args.days is not None else DEFAULT_COMP_DAYS
    if intended_days != DEFAULT_COMP_DAYS:
        print(
            f"WARNING: --days={intended_days} but the comp length is NOT stored "
            f"per-code; every redemption applies the fixed default of "
            f"{DEFAULT_COMP_DAYS} days. --days is recorded in the note only. To "
            f"enforce {intended_days} days for ALL invite comps, change "
            f"auth.invite_codes.DEFAULT_COMP_DAYS.",
            file=sys.stderr,
        )

    note_parts = [f"cohort={args.cohort}", f"comp_days={intended_days}"]
    if args.note:
        note_parts.append(args.note.strip())
    note = " ".join(note_parts)

    db = SessionLocal()
    try:
        # Reject a bad --for-user-id up front so codes aren't minted into the void
        # (mirrors the admin mint endpoint's 400).
        if args.for_user_id is not None:
            owner = db.query(User).filter(User.id == args.for_user_id).first()
            if owner is None:
                print(f"ERROR: --for-user-id={args.for_user_id} matches no user",
                      file=sys.stderr)
                return 1

        # Generate distinct codes; the unique index is the real guard but we
        # pre-dedupe within the batch + bound retries so a rare collision can't
        # spin forever. Mirrors api.invite_codes.mint_invite_codes.
        codes: list[str] = []
        seen: set[str] = set()
        attempts = 0
        while len(codes) < args.count and attempts < args.count * 10:
            attempts += 1
            code = generate_code()
            if code in seen:
                continue
            seen.add(code)
            db.add(BetaInviteCode(
                code=code,
                created_by_user_id=args.for_user_id,
                max_redemptions=args.max_redemptions,
                redemption_count=0,
                is_active=True,
                note=note,
            ))
            codes.append(code)
        db.commit()
    finally:
        db.close()

    if len(codes) < args.count:
        print(
            f"WARNING: only generated {len(codes)}/{args.count} codes "
            f"(code-collision retries exhausted)",
            file=sys.stderr,
        )

    for code in codes:
        print(code)

    print(
        f"Generated {len(codes)} single-use invite code(s) "
        f"(cohort={args.cohort}, intended {intended_days}d Pro comp, enforced "
        f"{DEFAULT_COMP_DAYS}d at redemption, max_redemptions={args.max_redemptions}).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
