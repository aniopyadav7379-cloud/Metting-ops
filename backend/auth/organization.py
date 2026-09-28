from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable, Optional

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from auth.models import Organization, User, UserOrganization

logger = logging.getLogger(__name__)

DEFAULT_ORG_SLUG = "magic-unicorn"
DEFAULT_ORG_NAME = "Magic Unicorn"
ORG_HEADER_NAME = "X-MeetingOps-Org"
ORG_QUERY_PARAM_NAMES = ("org", "org_slug")

GLOBAL_ADMIN_GROUPS = {
    "admin",
    "meeting-ops-admins",
    "meetingops-admin",
    "uc-admins",
    "app:meeting-ops-admin",
    "app:meetingops-admin",
    "app:meetingops:admin",
    "app:meeting-ops:admin",
}

ORG_ADMIN_PATTERN = re.compile(r"^app:meetingops:(?P<slug>[a-z0-9-]+):admin$")
NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


@dataclass
class ActiveOrganization:
    organization: Organization
    membership: Optional[UserOrganization]
    role: str

    @property
    def role_name(self) -> str:
        """Canonical normalized role accessor for authorization checks."""
        return (self.role or "").strip().lower()


def normalize_group_name(group: str) -> str:
    normalized = group.strip().lower()
    if normalized.startswith("role:"):
        normalized = normalized[5:]
    return normalized


def normalize_org_slug(value: str) -> str:
    slug = NON_SLUG_CHARS.sub("-", value.strip().lower()).strip("-")
    if not slug:
        raise ValueError("Organization slug cannot be empty")
    return slug


def display_name_from_slug(slug: str) -> str:
    return slug.replace("-", " ").title()


def parse_group_list(groups: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        normalized = normalize_group_name(group)
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def is_global_admin(groups: Iterable[str]) -> bool:
    normalized = set(parse_group_list(groups))
    return bool(normalized & GLOBAL_ADMIN_GROUPS)


def extract_org_roles(groups: Iterable[str]) -> dict[str, str]:
    roles_by_slug: dict[str, str] = {}

    for group in parse_group_list(groups):
        if group.startswith("org:"):
            slug = normalize_org_slug(group.split(":", 1)[1])
            roles_by_slug.setdefault(slug, "user")
            continue

        match = ORG_ADMIN_PATTERN.match(group)
        if match:
            slug = normalize_org_slug(match.group("slug"))
            roles_by_slug[slug] = "admin"

    return roles_by_slug


def ensure_organization(
    db: Session,
    slug: str,
    *,
    name: Optional[str] = None,
) -> Organization:
    normalized_slug = normalize_org_slug(slug)
    organization = (
        db.query(Organization)
        .filter(Organization.slug == normalized_slug)
        .first()
    )
    if organization:
        return organization

    organization = Organization(
        name=name or display_name_from_slug(normalized_slug),
        slug=normalized_slug,
        is_active=True,
    )
    db.add(organization)
    db.commit()
    db.refresh(organization)
    return organization


def ensure_default_organization(db: Session) -> Organization:
    return ensure_organization(db, DEFAULT_ORG_SLUG, name=DEFAULT_ORG_NAME)


def load_organization(db: Session, org_id: int) -> Optional[Organization]:
    """Fetch an Organization by primary key, or None.

    Convenience for code paths (agent-write tools, WebSocket handlers) that
    hold an ``org_id`` / ``session.organization_id`` and need the Organization
    row to run a per-workspace billing gate (the ``active_org`` argument to
    ``auth.tier.gate_feature_for_caller``)."""
    return db.query(Organization).filter(Organization.id == org_id).first()


def sync_user_organizations(
    db: Session,
    user: User,
    groups: Iterable[str],
) -> list[UserOrganization]:
    org_roles = extract_org_roles(groups)

    # If we have NO group claims to act on, treat this as a "passive" sync —
    # do not touch the user's existing memberships. The empty-groups case
    # happens both for genuinely-new users AND for any code path that calls
    # this without forwarding the Keycloak groups header (api/auth/me through
    # forward-auth, internal helpers, tests). Wiping existing memberships in
    # the latter case is destructive.
    existing_memberships = (
        db.query(UserOrganization)
        .filter(UserOrganization.user_id == user.id)
        .all()
    )

    if not org_roles:
        if existing_memberships:
            # Already a member of at least one org. Leave them alone.
            return existing_memberships
        # Brand-new user, no group claims. Provision a private personal org
        # instead of dumping them into the platform default workspace where
        # they would see another tenant's sessions.
        personal_slug = f"{user.username}-personal"
        existing_org = (
            db.query(Organization)
            .filter(Organization.slug == personal_slug)
            .first()
        )
        if not existing_org:
            display = user.full_name or user.email or user.username
            ensure_organization(
                db,
                personal_slug,
                name=f"{display} (personal)",
            )
        org_roles = {personal_slug: "admin"}

    desired_roles: dict[int, str] = {}
    for slug, role in org_roles.items():
        organization = ensure_organization(db, slug)
        desired_roles[organization.id] = role

    existing_by_org_id = {
        membership.organization_id: membership
        for membership in existing_memberships
    }

    changed = False

    # Sync is additive: Keycloak group claims grant + refresh memberships,
    # they do NOT prune. Manual DB-level grants (set by admins outside the
    # group-claim flow) survive subsequent logins. To remove a membership,
    # delete the row directly or build an explicit admin endpoint — don't
    # rely on "absent claim" as the deletion signal.
    for organization_id, role in desired_roles.items():
        membership = existing_by_org_id.get(organization_id)
        if membership:
            if membership.role != role:
                membership.role = role
                changed = True
            continue

        db.add(
            UserOrganization(
                user_id=user.id,
                organization_id=organization_id,
                role=role,
            )
        )
        changed = True

    if changed:
        db.commit()

    db.refresh(user)
    return (
        db.query(UserOrganization)
        .filter(UserOrganization.user_id == user.id)
        .all()
    )


def get_requested_org_slug(request: Request) -> Optional[str]:
    header_value = request.headers.get(ORG_HEADER_NAME)
    if header_value:
        return normalize_org_slug(header_value)

    # Browser WebSockets and media tags cannot set custom headers, so allow
    # an explicit query-string selector for those transports.
    for name in ORG_QUERY_PARAM_NAMES:
        query_value = request.query_params.get(name)
        if query_value:
            return normalize_org_slug(query_value)

    return None


def resolve_active_organization(
    db: Session,
    request: Request,
    current_user: User,
) -> ActiveOrganization:
    requested_slug = get_requested_org_slug(request)

    # An explicit org selector (X-MeetingOps-Org header / ?org=) is a
    # CONVENIENCE — "which of my workspaces am I looking at" — never a security
    # boundary (that's the membership check at the bottom of this function). A
    # stale or malformed selector must therefore never lock the user out of the
    # app. The iOS client (build <=7) persisted the numeric org *id* where a
    # *slug* is expected (OrgSwitcherView stored org.id, APIClient sent it as
    # X-MeetingOps-Org), so every request carried an unmatchable selector and
    # this branch used to hard-404 the user off the launch screen. We now
    # degrade gracefully: an unknown or inaccessible selector is logged and
    # dropped, and resolution falls through to the user's default workspace
    # exactly as if no selector had been sent.
    organization = None
    if requested_slug:
        organization = (
            db.query(Organization)
            .filter(Organization.slug == requested_slug)
            .first()
        )
        if organization is None:
            logger.warning(
                "org selector %r matched no organization for user id=%s; "
                "falling back to default resolution",
                requested_slug,
                current_user.id,
            )
        elif not current_user.is_superuser and not any(
            m.organization_id == organization.id
            for m in current_user.organizations
        ):
            logger.warning(
                "org selector %r not accessible to user id=%s; "
                "falling back to default resolution",
                requested_slug,
                current_user.id,
            )
            organization = None

    if organization is None and current_user.organizations:
        # No usable explicit selector. Resolve the active org through a
        # preference chain (each step only applies if the user is actually a
        # member, so the no-403 behavior is always preserved):
        #
        #   1. The user's server-side "home" workspace
        #      (User.default_organization_id) — persists their chosen
        #      workspace across devices + on any node. This replaces the
        #      dogfood-specific DEFAULT_ORG_SLUG hack as the TOP preference.
        #   2. The platform "home" org (DEFAULT_ORG_SLUG) when they're a
        #      member — kept as a fallback so existing dogfood users with no
        #      default set yet still land on magic-unicorn instead of an
        #      empty/test org that happens to be first.
        #   3. The first membership — for users in neither of the above
        #      (e.g. GFL/shafen-khan), preserving the no-403 behavior.
        default_org_id = getattr(current_user, "default_organization_id", None)
        if default_org_id is not None:
            organization = next(
                (
                    m.organization
                    for m in current_user.organizations
                    if m.organization and m.organization.id == default_org_id
                ),
                None,
            )
        if organization is None:
            organization = next(
                (
                    m.organization
                    for m in current_user.organizations
                    if m.organization and m.organization.slug == DEFAULT_ORG_SLUG
                ),
                current_user.organizations[0].organization,
            )
    elif organization is None:
        # User has no memberships yet; auto-provision into the platform default.
        organization = ensure_default_organization(db)

    membership = (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id == current_user.id,
            UserOrganization.organization_id == organization.id,
        )
        .first()
    )

    if not membership and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this organization",
        )

    role = "superuser" if current_user.is_superuser else (membership.role if membership else "viewer")
    return ActiveOrganization(
        organization=organization,
        membership=membership,
        role=role,
    )
