"""Launch admin: comps + invite-code oversight (PLATFORM-SUPERUSER ONLY).

The "place to see and organize those specific subscriptions" for the launch
cohort. Surfaces every non-free user (bounded comps, permanent/paid, and
superusers) plus grant/revoke actions, so an operator can run the free / $1
launch program from inside Meeting-Ops instead of shelling into the container
with scripts/grant_pro.py.

Routes (all superuser-gated — see `require_superuser`; do NOT use require_admin,
which every self-serve user satisfies for their own personal org):

  GET  /api/admin/comps            list non-free users (filter: status/cohort/q)
  GET  /api/admin/comps/summary    dashboard counts (comps + invite codes)
  POST /api/admin/comps/grant      grant/extend a time-limited comp by email
  POST /api/admin/comps/revoke     revert a comp to free by email

The invite-code LIST lives next to its minter in api/invite_codes.py
(GET /api/admin/invite-codes). Grant reuses the SAME shared helper the
invite-redemption path uses (auth.invite_codes.comp_personal_org_to_pro) so a
manual comp and a redeemed-code comp are byte-for-byte identical on both gated
surfaces (user.tier + tier_expires_at AND the personal org's plan). Revoke
mirrors scripts/grant_pro.apply_comp + services.session_watchdog.revert_expired_comps
exactly (tier->free + expiry cleared + personal org plan->free; founding flags
left alone). Every mutation is audit-logged via AuthService.log_action.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user
from auth.invite_codes import (
    DEFAULT_COMP_DAYS,
    _resolve_personal_org,
    comp_personal_org_to_pro,
)
from auth.models import BetaInviteCode, Organization, User, UserOrganization
from auth.service import AuthService
from database.database import get_db

logger = logging.getLogger(__name__)

admin_router = APIRouter(prefix="/api/admin", tags=["admin-comps"])

# Tiers a comp may grant. Mirrors scripts/grant_pro._GRANTABLE_TIERS (the paid
# names in auth.tier._TIER_RANK); 'free' is only reachable via /revoke.
_GRANTABLE_TIERS = ("basic", "pro", "suite", "enterprise")


async def require_superuser(
    current_user: User = Depends(get_current_user),
) -> User:
    """Hard platform-superuser gate.

    NOT require_admin: every self-serve user is admin of their own
    {username}-personal org, so require_admin would let anyone in. The launch
    console is a true platform surface — only a superuser sees the whole
    cohort. Mirrors the explicit is_superuser check on the invite-code minter.
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Platform superuser only",
        )
    return current_user


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------
class CompUser(BaseModel):
    id: int
    email: str
    username: str
    full_name: Optional[str] = None
    tier: str
    # active | expired | permanent | superuser
    status: str
    tier_expires_at: Optional[datetime] = None
    days_remaining: Optional[int] = None  # floor of days; negative once expired
    is_founding_member: bool = False
    founding_cohort: Optional[str] = None
    has_stripe: bool = False  # stripe_customer_id present (paying / portal-touched)
    personal_org_slug: Optional[str] = None
    personal_org_plan: Optional[str] = None
    created_at: Optional[datetime] = None


class CompsSummary(BaseModel):
    total_nonfree: int
    active_comps: int
    expired_pending_revert: int
    permanent: int
    superusers: int
    by_cohort: dict[str, int]
    codes_total: int
    codes_redeemed: int
    codes_available: int


class CompSnapshot(BaseModel):
    email: str
    tier: str
    tier_expires_at: Optional[str] = None
    is_founding_member: bool = False
    founding_cohort: Optional[str] = None
    personal_org_slug: Optional[str] = None
    personal_org_plan: Optional[str] = None


class CompActionResponse(BaseModel):
    action: str  # grant | revoke
    before: CompSnapshot
    after: CompSnapshot


class GrantCompRequest(BaseModel):
    email: str
    tier: str = "pro"
    days: int = Field(default=DEFAULT_COMP_DAYS, ge=1, le=3650)
    founding: bool = False
    cohort: str = "meeting_ops_v1"


class RevokeCompRequest(BaseModel):
    email: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly-naive datetime to UTC-aware for comparison."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _status_for(user: User, now: datetime) -> str:
    if user.is_superuser:
        return "superuser"
    exp = _aware(user.tier_expires_at)
    if exp is None:
        return "permanent"  # real Stripe sub clears the expiry, or a manual permanent tier
    return "active" if exp > now else "expired"


def _days_remaining(user: User, now: datetime) -> Optional[int]:
    exp = _aware(user.tier_expires_at)
    if exp is None:
        return None
    secs = (exp - now).total_seconds()
    return math.floor(secs / 86400)


def _snapshot(user: User, org: Optional[Organization]) -> CompSnapshot:
    """Mirror scripts/grant_pro._snapshot so before/after matches the CLI."""
    exp = user.tier_expires_at
    return CompSnapshot(
        email=user.email,
        tier=user.tier,
        tier_expires_at=exp.isoformat() if exp else None,
        is_founding_member=bool(user.is_founding_member),
        founding_cohort=user.founding_cohort,
        personal_org_slug=org.slug if org is not None else None,
        personal_org_plan=(org.plan or "free") if org is not None else None,
    )


def _org_map(db: Session, users: list[User]) -> dict[int, Organization]:
    """Batch-resolve each user's personal org (3 queries, no N+1).

    Mirrors auth.invite_codes._resolve_personal_org: prefer an admin
    membership, else the '{username}-personal' slug.
    """
    if not users:
        return {}
    uids = [u.id for u in users]
    uid_to_orgid: dict[int, int] = {}
    for m in (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id.in_(uids),
            UserOrganization.role == "admin",
        )
        .all()
    ):
        uid_to_orgid.setdefault(m.user_id, m.organization_id)

    org_ids = set(uid_to_orgid.values())
    orgs_by_id: dict[int, Organization] = {}
    if org_ids:
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all():
            orgs_by_id[o.id] = o

    # Slug fallback only for users without an admin membership.
    slug_users = [u for u in users if u.id not in uid_to_orgid]
    orgs_by_slug: dict[str, Organization] = {}
    if slug_users:
        slugs = [f"{u.username}-personal" for u in slug_users]
        for o in db.query(Organization).filter(Organization.slug.in_(slugs)).all():
            orgs_by_slug[o.slug] = o

    result: dict[int, Organization] = {}
    for u in users:
        oid = uid_to_orgid.get(u.id)
        if oid is not None and oid in orgs_by_id:
            result[u.id] = orgs_by_id[oid]
        else:
            org = orgs_by_slug.get(f"{u.username}-personal")
            if org is not None:
                result[u.id] = org
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@admin_router.get("/comps", response_model=list[CompUser])
async def list_comps(
    status_filter: Optional[str] = Query(
        default=None,
        alias="status",
        description="active | expired | permanent | superuser",
    ),
    cohort: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None, description="email/username substring"),
    limit: int = Query(default=500, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(require_superuser),
    db: Session = Depends(get_db),
) -> list[CompUser]:
    """Every non-free user, newest comp first. The launch cohort lives here."""
    query = db.query(User).filter(User.tier != "free")
    if cohort:
        query = query.filter(User.founding_cohort == cohort)
    if q:
        like = f"%{q.strip().lower()}%"
        query = query.filter(
            (func.lower(User.email).like(like)) | (func.lower(User.username).like(like))
        )
    # Sort: soonest-expiring first (active comps that need attention), then
    # permanent/superuser (NULL expiry) last.
    users = (
        query.order_by(User.tier_expires_at.asc().nullslast(), User.id.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    now = datetime.now(timezone.utc)
    orgs = _org_map(db, users)
    rows: list[CompUser] = []
    for u in users:
        st = _status_for(u, now)
        if status_filter and st != status_filter:
            continue
        org = orgs.get(u.id)
        rows.append(
            CompUser(
                id=u.id,
                email=u.email,
                username=u.username,
                full_name=u.full_name,
                tier=u.tier,
                status=st,
                tier_expires_at=u.tier_expires_at,
                days_remaining=_days_remaining(u, now),
                is_founding_member=bool(u.is_founding_member),
                founding_cohort=u.founding_cohort,
                has_stripe=bool(u.stripe_customer_id),
                personal_org_slug=org.slug if org is not None else None,
                personal_org_plan=(org.plan or "free") if org is not None else None,
                created_at=u.created_at,
            )
        )
    return rows


@admin_router.get("/comps/summary", response_model=CompsSummary)
async def comps_summary(
    current_user: User = Depends(require_superuser),
    db: Session = Depends(get_db),
) -> CompsSummary:
    """Dashboard counts for the launch console header."""
    now = datetime.now(timezone.utc)
    users = db.query(User).filter(User.tier != "free").all()

    active = expired = permanent = supers = 0
    by_cohort: dict[str, int] = {}
    for u in users:
        st = _status_for(u, now)
        if st == "active":
            active += 1
        elif st == "expired":
            expired += 1
        elif st == "permanent":
            permanent += 1
        elif st == "superuser":
            supers += 1
        if u.founding_cohort:
            by_cohort[u.founding_cohort] = by_cohort.get(u.founding_cohort, 0) + 1

    codes_total = db.query(func.count(BetaInviteCode.id)).scalar() or 0
    codes_redeemed = (
        db.query(func.count(BetaInviteCode.id))
        .filter(BetaInviteCode.redemption_count > 0)
        .scalar()
        or 0
    )
    codes_available = (
        db.query(func.count(BetaInviteCode.id))
        .filter(
            BetaInviteCode.is_active.is_(True),
            BetaInviteCode.redemption_count < BetaInviteCode.max_redemptions,
        )
        .scalar()
        or 0
    )

    return CompsSummary(
        total_nonfree=len(users),
        active_comps=active,
        expired_pending_revert=expired,
        permanent=permanent,
        superusers=supers,
        by_cohort=by_cohort,
        codes_total=int(codes_total),
        codes_redeemed=int(codes_redeemed),
        codes_available=int(codes_available),
    )


def _lookup_user(db: Session, email: str) -> User:
    """Case-insensitive email lookup (mirrors scripts/grant_pro.main)."""
    target = (
        db.query(User).filter(func.lower(User.email) == email.strip().lower()).first()
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No user with email={email!r}",
        )
    return target


@admin_router.post("/comps/grant", response_model=CompActionResponse)
async def grant_comp(
    payload: GrantCompRequest,
    current_user: User = Depends(require_superuser),
    db: Session = Depends(get_db),
) -> CompActionResponse:
    """Grant (or extend) a time-limited comp to an existing user by email.

    Reuses the SAME shared helper the invite-redemption path uses so a manual
    comp is identical to a redeemed-code comp on both gated surfaces. Re-running
    a grant refreshes the expiry window (idempotent extend)."""
    if payload.tier not in _GRANTABLE_TIERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tier must be one of {_GRANTABLE_TIERS}",
        )
    target = _lookup_user(db, payload.email)
    before = _snapshot(target, _resolve_personal_org(db, target))

    # Canonical grant: user.tier + tier_expires_at (now + days) AND personal org
    # plan=<tier> + max_monthly_hours cleared. Commits internally.
    comp_personal_org_to_pro(db, target, tier=payload.tier, days=payload.days)
    if payload.founding:
        target.is_founding_member = True
        if payload.cohort:
            target.founding_cohort = payload.cohort
        db.add(target)
        db.commit()
    db.refresh(target)

    org = _resolve_personal_org(db, target)
    after = _snapshot(target, org)

    AuthService.log_action(
        db,
        current_user.id,
        "comp_granted",
        resource_type="user",
        resource_id=str(target.id),
        details={
            "email": target.email,
            "tier": payload.tier,
            "days": payload.days,
            "founding": payload.founding,
            "cohort": payload.cohort if payload.founding else None,
        },
    )
    logger.info(
        "admin comp_granted by superuser_id=%s -> user_id=%s tier=%s days=%s",
        current_user.id, target.id, payload.tier, payload.days,
    )
    return CompActionResponse(action="grant", before=before, after=after)


@admin_router.post("/comps/revoke", response_model=CompActionResponse)
async def revoke_comp(
    payload: RevokeCompRequest,
    current_user: User = Depends(require_superuser),
    db: Session = Depends(get_db),
) -> CompActionResponse:
    """Revert a comp to free. Mirrors grant_pro --revoke + the auto-revert
    watchdog exactly: user.tier->free + expiry cleared + personal org plan->free;
    founding flags left alone. A superuser can't be revoked into a broken state
    (their tier resolves to enterprise regardless)."""
    target = _lookup_user(db, payload.email)
    org = _resolve_personal_org(db, target)
    before = _snapshot(target, org)

    target.tier = "free"
    target.tier_expires_at = None
    db.add(target)
    if org is not None and (org.plan or "free") != "free":
        org.plan = "free"
        db.add(org)
    db.commit()
    db.refresh(target)
    if org is not None:
        db.refresh(org)
    after = _snapshot(target, org)

    AuthService.log_action(
        db,
        current_user.id,
        "comp_revoked",
        resource_type="user",
        resource_id=str(target.id),
        details={"email": target.email},
    )
    logger.info(
        "admin comp_revoked by superuser_id=%s -> user_id=%s",
        current_user.id, target.id,
    )
    return CompActionResponse(action="revoke", before=before, after=after)
