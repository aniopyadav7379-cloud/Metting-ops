#!/usr/bin/env python3
"""Grant (or revoke) a TIME-LIMITED Pro/paid comp for a Meeting-Ops user.

The comp is bounded and set on BOTH surfaces that require_feature() (auth/tier.py)
gates paid server-compute on — otherwise a user-only comp still 403'd (billing-1):

  USER side: ``User.tier`` + ``User.tier_expires_at = now + N days``.
  ORG side:  the user's PERSONAL org gets ``plan=<tier>`` +
             ``max_monthly_hours=None`` (mirrors comp_personal_org_to_pro /
             api.stripe_webhook._apply_org_plan_from_sub). The org is resolved
             with auth.invite_codes._resolve_personal_org; a user with no
             personal org still gets the user-side comp (a warning is printed).

The session-watchdog cron
(``services.session_watchdog.revert_expired_comps``) reverts the user to
'free' AND drops the personal org back to 'free' once the expiry passes, so a
"free month" comp for an invited cohort needs no Stripe/card and can't silently
become permanent. A real subscription later clears ``tier_expires_at``
(``api.stripe_webhook``), so a comped user who then pays keeps Pro.

Usage (inside meet-backend, or from backend/ with DATABASE_URL set):
    python -m scripts.grant_pro alice@example.com --days 30
    python -m scripts.grant_pro alice@example.com --days 30 --tier pro \
        --founding --cohort meeting_ops_v1
    python -m scripts.grant_pro alice@example.com --revoke

Idempotent: re-running a grant refreshes the expiry window; --revoke on an
already-free user is a no-op. Prints a before/after JSON snapshot (includes the
personal org's plan).

Note: this is a deliberate MANUAL admin grant. Unlike the Stripe path it does
NOT enforce the Founding 100 cap when --founding is passed — the operator owns
that decision.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Tiers a comp may grant. Mirrors the paid names in auth.tier._TIER_RANK;
# 'free' is only reachable via --revoke.
_GRANTABLE_TIERS = ("basic", "pro", "suite", "enterprise")


def _snapshot(user, org=None) -> dict:
    """Serializable view of the comp-relevant columns for before/after output.

    ``org`` is the user's resolved personal org (or None). Its plan is included
    so the before/after shows BOTH surfaces the comp touches (user tier + org
    plan)."""
    expires = getattr(user, "tier_expires_at", None)
    return {
        "email": user.email,
        "tier": user.tier,
        "tier_expires_at": expires.isoformat() if expires else None,
        "is_founding_member": bool(getattr(user, "is_founding_member", False)),
        "founding_cohort": getattr(user, "founding_cohort", None),
        "personal_org_slug": getattr(org, "slug", None) if org is not None else None,
        "personal_org_plan": (
            (getattr(org, "plan", None) or "free") if org is not None else None
        ),
    }


def apply_comp(
    db,
    user,
    *,
    revoke: bool,
    tier: str = "pro",
    days: int = 30,
    founding: bool = False,
    cohort: str = "meeting_ops_v1",
):
    """Mutate ``user`` (and their personal org) in place per the flags and commit.

    Returns ``(before, after)`` snapshot dicts. Factored out of ``main()`` so it
    is unit-testable without argv.

    GRANT: user.tier=<tier> + a fresh ``now + days`` expiry (+ optional founding
    flag/cohort), AND the personal org plan=<tier> + max_monthly_hours cleared —
    both surfaces require_feature() gates on, so a user-only comp still 403'd
    (billing-1). REVOKE: user.tier='free' + expiry cleared, AND the personal org
    plan back to 'free'. Founding flags are left alone on revoke, mirroring the
    auto-revert watchdog's surgical scope.

    The personal org is resolved with auth.invite_codes._resolve_personal_org
    (the same helper the invite-comp + watchdog use, so grant/revert/redeem all
    target the same org). A user with no personal org still gets the user-side
    change; a warning is printed to stderr.
    """
    # Lazy import: keeps grant_pro's module top import-light (stdlib only) and
    # avoids coupling the script's import to the auth package.
    from auth.invite_codes import _resolve_personal_org

    org = _resolve_personal_org(db, user)
    before = _snapshot(user, org)
    if revoke:
        user.tier = "free"
        user.tier_expires_at = None
        # billing-1: drop the personal org back to the free plan so the
        # per-workspace gate matches the user revert. max_monthly_hours is left
        # NULL (the free-tier default applies via services.quotas).
        if org is not None and (org.plan or "free") != "free":
            org.plan = "free"
            db.add(org)
    else:
        user.tier = tier
        user.tier_expires_at = datetime.now(timezone.utc) + timedelta(days=days)
        if founding:
            user.is_founding_member = True
            if cohort:
                user.founding_cohort = cohort
        # billing-1: comp the personal org too (plan=<tier> + clear the per-org
        # hours override) — mirrors comp_personal_org_to_pro /
        # stripe_webhook._apply_org_plan_from_sub.
        if org is not None:
            org.plan = tier
            org.max_monthly_hours = None
            db.add(org)
    if org is None:
        print(
            f"WARNING: no personal org found for {user.email!r} "
            f"(looked for an admin membership / '{user.username}-personal' slug); "
            f"comped the user tier only, org plan unchanged.",
            file=sys.stderr,
        )
    db.add(user)
    db.commit()
    db.refresh(user)
    if org is not None:
        db.refresh(org)
    return before, _snapshot(user, org)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Grant/revoke a time-limited Pro comp for a Meeting-Ops user",
    )
    ap.add_argument("email", help="email of the user to comp")
    ap.add_argument("--days", type=int, default=30,
                    help="comp length in days (default 30)")
    ap.add_argument("--tier", default="pro", choices=_GRANTABLE_TIERS,
                    help="tier to grant (default pro)")
    ap.add_argument("--founding", action="store_true",
                    help="also set is_founding_member (perks overlay)")
    ap.add_argument("--cohort", default="meeting_ops_v1",
                    help="founding_cohort label when --founding (default meeting_ops_v1)")
    ap.add_argument("--revoke", action="store_true",
                    help="revert to free + clear expiry (ignores --days/--tier)")
    args = ap.parse_args()

    if not args.revoke and args.days < 1:
        print("ERROR: --days must be >= 1 (use --revoke to remove a comp)",
              file=sys.stderr)
        return 2

    from sqlalchemy import func

    from auth.models import User
    from database.database import SessionLocal

    email = args.email.strip().lower()
    db = SessionLocal()
    try:
        # Case-insensitive lookup: emails are stored lowercased on the Stripe
        # path but a manually-created user could differ.
        user = (
            db.query(User)
            .filter(func.lower(User.email) == email)
            .first()
        )
        if user is None:
            print(f"ERROR: no user with email={email!r}", file=sys.stderr)
            return 1
        before, after = apply_comp(
            db,
            user,
            revoke=args.revoke,
            tier=args.tier,
            days=args.days,
            founding=args.founding,
            cohort=args.cohort,
        )
        print(json.dumps(
            {
                "action": "revoke" if args.revoke else "grant",
                "before": before,
                "after": after,
            },
            indent=2,
        ))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
