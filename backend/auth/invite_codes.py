"""Phase-1 beta invite-code helpers.

Shared by the self-serve register hook (auth/routes.py) and the invite-code
router (api/invite_codes.py) so the validate / atomic-consume / personal-org
comp logic lives in exactly one place.

Flow (self-serve register, when a code is required OR optionally supplied):
  1. register() resolves + validates the code BEFORE create_user
     (validate_invite_code) -> 403 if missing / unknown / inactive /
     exhausted. No row is mutated here. A code is validated when
     REQUIRE_INVITE_CODE is on (then it is required) OR when the user
     supplies one while the gate is off (an OPTIONAL code -> Pro comp).
  2. create_user() runs. A failed create raises before step 3, so a failed
     signup never burns a code.
  3. register() consumes the code ATOMICALLY (consume_invite_code):
     UPDATE ... WHERE id=:id AND redemption_count < max_redemptions and
     checks rowcount, so two parallel registrations can't both claim the
     last seat. The loser gets rowcount=0 and is treated as exhausted.
  4. register() comps the new user (comp_personal_org_to_pro) to a
     TIME-LIMITED Pro tier on BOTH surfaces that require_feature() gates on:
     the USER (user.tier='pro' + user.tier_expires_at = now + N days) AND the
     user's PERSONAL org (plan='pro' + max_monthly_hours=None), mirroring
     api/stripe_webhook._apply_org_plan_from_sub. Setting only one still 403'd
     paid server-compute (billing-1); the expiry lets session_watchdog
     auto-revert an unpaid comp so a "free month" can't become permanent.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from auth.models import BetaInviteCode, Organization, User, UserOrganization

logger = logging.getLogger(__name__)

# Unambiguous code alphabet (no 0/O/1/I/l) so codes are easy to read aloud
# / type from a hand-off. 10 chars from a 31-char alphabet ≈ 49 bits.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 10

# Default length of an invite-code Pro comp, in days. BetaInviteCode carries no
# per-code duration column, so every redemption applies THIS default at
# redemption time (threaded through comp_personal_org_to_pro's `days` param).
# It is the single source of truth for the invite-comp window: change it here
# (or add a per-code column + migration) to change the enforced length. Kept in
# lockstep with scripts/gen_invite_codes.py, which stamps the intended value
# into each code's note for cohort tracking.
DEFAULT_COMP_DAYS = 30


def generate_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def validate_invite_code(db: Session, raw_code: Optional[str]) -> BetaInviteCode:
    """Resolve + validate an invite code for redemption.

    Returns the row when the code exists, is active, and still has
    redemptions left. Raises ValueError otherwise (the caller maps that to a
    403). Intentionally does NOT mutate anything — consumption is a separate,
    atomic step that runs only after create_user succeeds.
    """
    code = (raw_code or "").strip()
    if not code:
        raise ValueError("A valid invite code is required.")
    row = (
        db.query(BetaInviteCode)
        .filter(BetaInviteCode.code == code)
        .first()
    )
    if row is None:
        raise ValueError("A valid invite code is required.")
    if not row.is_active:
        raise ValueError("A valid invite code is required.")
    if row.redemption_count >= row.max_redemptions:
        raise ValueError("A valid invite code is required.")
    return row


def consume_invite_code(db: Session, code_id: int, redeemed_by_user_id: int) -> bool:
    """Atomically claim one redemption of the code.

    Single UPDATE guarded by `redemption_count < max_redemptions` so a race
    can't double-redeem a single-use code: whichever transaction commits
    first wins, the other sees rowcount == 0. Stamps redeemed_by/redeemed_at
    and flips is_active False once the code is exhausted. Returns True iff
    this call claimed a seat.
    """
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(BetaInviteCode)
        .where(BetaInviteCode.id == code_id)
        .where(BetaInviteCode.redemption_count < BetaInviteCode.max_redemptions)
        .values(
            redemption_count=BetaInviteCode.redemption_count + 1,
            redeemed_by_user_id=redeemed_by_user_id,
            redeemed_at=now,
            # Exhausted once this redemption takes it to the cap. The +1 is the
            # value AFTER this update; compare against max_redemptions.
            is_active=(BetaInviteCode.redemption_count + 1 < BetaInviteCode.max_redemptions),
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return (result.rowcount or 0) > 0


def _resolve_personal_org(db: Session, user: User) -> Optional[Organization]:
    """Find the new user's personal org to comp.

    Prefers an admin membership (create_user(personal_org=True) makes the
    user admin of `{username}-personal`); falls back to the
    `{username}-personal` slug. Returns None if neither resolves (logged,
    non-fatal — the user is still created, just not comped)."""
    membership = (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id == user.id,
            UserOrganization.role == "admin",
        )
        .first()
    )
    if membership is not None:
        org = (
            db.query(Organization)
            .filter(Organization.id == membership.organization_id)
            .first()
        )
        if org is not None:
            return org
    return (
        db.query(Organization)
        .filter(Organization.slug == f"{user.username}-personal")
        .first()
    )


def comp_personal_org_to_pro(
    db: Session,
    user: User,
    *,
    tier: str = "pro",
    days: int = DEFAULT_COMP_DAYS,
) -> bool:
    """Comp a redeeming user to a TIME-LIMITED paid tier on BOTH gated surfaces.

    require_feature() (auth/tier.py) gates paid server-compute on BOTH the
    user's global tier AND the ACTIVE org's plan, so a comp that moved only one
    of them still 403'd — the exact bug this fixes. We set both, bounded by an
    expiry so the auto-revert (services.session_watchdog.revert_expired_comps)
    can undo an unpaid comp:

      USER side (always applied — it's the passed-in object): user.tier=<tier>
        (default 'pro') + user.tier_expires_at = now + <days> (default
        DEFAULT_COMP_DAYS). The expiry makes an invite comp a bounded "free
        month", never a silent permanent freebie; a real Stripe subscription
        later CLEARS the expiry (api.stripe_webhook), so a comped user who then
        pays keeps the tier.
      ORG side (best-effort): the user's PERSONAL org gets plan='pro' +
        max_monthly_hours=None — mirrors api/stripe_webhook._apply_org_plan_from_sub
        EXACTLY for the Pro case (clear any per-org override so services.quotas
        falls through to the Pro default). A missing org is logged, not fatal.

    Returns True iff a personal org was found + comped (the user side succeeds
    either way).
    """
    now = datetime.now(timezone.utc)
    user.tier = tier
    user.tier_expires_at = now + timedelta(days=days)
    db.add(user)

    org = _resolve_personal_org(db, user)
    if org is None:
        # User side still committed so the tier gate opens; only the
        # per-workspace org gate is left unset (logged for follow-up).
        db.commit()
        logger.warning(
            "invite-comp: no personal org found for user_id=%s username=%s; "
            "user.tier->%s (expires %s) set, but org plan NOT comped to pro",
            user.id, user.username, tier,
            user.tier_expires_at.isoformat(),
        )
        return False
    org.plan = "pro"
    org.max_monthly_hours = None
    db.add(org)
    db.commit()
    logger.info(
        "invite-comp: user_id=%s tier->%s (expires %s) + org_id=%s plan->pro "
        "(max_monthly_hours cleared)",
        user.id, tier, user.tier_expires_at.isoformat(), org.id,
    )
    return True
