from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Float,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from database.database import Base
import os

# Use JSONB for PostgreSQL, JSON for others
def get_json_type():
    if "postgresql" in os.getenv("DATABASE_URL", ""):
        return JSONB
    return JSON

class Organization(Base):
    __tablename__ = "organizations"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)  # URL-friendly name
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Plan and limits. NULL on max_file_bytes / max_concurrent_uploads means
    # "use the tier default from services.quotas.TIER_DEFAULTS". Use the
    # helpers in services/quotas.py rather than reading these columns
    # directly so per-org overrides and tier defaults compose correctly.
    plan = Column(String, default="free")  # free, pro, enterprise
    storage_quota_gb = Column(Integer, default=10)
    storage_used_gb = Column(Float, default=0.0)
    max_users = Column(Integer, default=5)
    # max_monthly_hours / max_file_bytes / max_concurrent_uploads are NULL by
    # default so services.quotas falls through to tier defaults. Existing
    # orgs that were created with the old default of 100 keep their value
    # (the migration does not retroactively NULL it).
    max_monthly_hours = Column(Integer, nullable=True)
    max_file_bytes = Column(BigInteger, nullable=True)
    max_concurrent_uploads = Column(Integer, nullable=True)
    
    # Settings
    settings = Column(get_json_type(), default={})
    is_active = Column(Boolean, default=True)

    # Per-org integration toggles + credentials (v3.19.0).
    # Shape (each key optional; absent = use env-var default + disabled):
    #   {
    #     "brigade":        {"enabled": bool, "api_base_url": str, "api_key_encrypted": str,
    #                        "tenancy_mode": "shared"|"per_org_graph"|"per_org_instance",
    #                        "shared_agent_id": str},
    #     "project_ops":    {"enabled": bool, "api_base_url": str, "api_key_encrypted": str,
    #                        "default_project_number": str},
    #     "contact_ops":    {"enabled": bool, "api_base_url": str, "api_key_encrypted": str},
    #     "accounting_ops": {"enabled": bool, "api_base_url": str, "api_key_encrypted": str},
    #     "stable":         {"enabled": bool, "api_base_url": str, "api_key_encrypted": str},
    #   }
    # api_key_encrypted is Fernet ciphertext (services.providers.crypto).
    # NEVER return the cleartext value from any HTTP response. Always mask
    # via api.integrations_org._mask_integration before serializing.
    integrations = Column(get_json_type(), nullable=False, default=dict, server_default="{}")

    # Stripe Subscriptions: when org-level billing lands this column links
    # the Organization to its Stripe Subscription object. v1 is individual
    # billing (the link lives on User.stripe_customer_id) so this stays
    # NULL until the org-billing flow ships. Added pre-emptively in
    # alembic 033 so we don't need a second migration.
    stripe_subscription_id = Column(String(64), unique=True, nullable=True, index=True)

    # uc-registry workspace UUID this org maps to (SUITE-IDENTITY 0.1).
    # Inbound Brigade federation tokens assert tenant via the
    # `workspace_id` claim; api.federation_meetings binds it to org_id
    # through THIS column (never a header). Nullable + unique: existing
    # orgs stay NULL until explicitly mapped. Added in alembic 041.
    workspace_id = Column(String(64), unique=True, nullable=True, index=True)


    # Relationships
    users = relationship("UserOrganization", back_populates="organization")

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    
    # Profile
    full_name = Column(String)
    avatar_url = Column(String)
    phone = Column(String)
    department = Column(String)
    
    # Status
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    is_superuser = Column(Boolean, default=False)

    # Tier-based feature gating. One of 'free' / 'pro' / 'enterprise'.
    # Source of truth for what each tier unlocks lives in
    # backend/auth/tier.py (TIER_FEATURES dict). Backfilled by alembic
    # 028 to set Aaron + Shafen to enterprise. New users default to
    # 'free' until billing flips them.
    tier = Column(String(32), nullable=False, default="free")

    # Time-limited Pro/paid comp expiry (added in alembic 051). When set, the
    # user's `tier` above is a *bounded* grant: the session_watchdog cron
    # (services.session_watchdog.revert_expired_comps) reverts `tier` to 'free'
    # and clears this column once `tier_expires_at < now()`. This lets an admin
    # comp an invited cohort a "free month" of Pro (scripts/grant_pro.py) with
    # no Stripe/card, and guarantees the comp can't silently become permanent.
    # NULL = permanent tier: a real Stripe subscription CLEARS this column
    # (api.stripe_webhook._handle_subscription_created/_updated) so a paying
    # customer — or a comped user who later subscribes — is never auto-reverted.
    tier_expires_at = Column(DateTime(timezone=True), nullable=True)

    # Stripe customer ID (cus_...). NULL until the user first touches
    # Checkout / Billing Portal, or until a `customer.created` webhook
    # event arrives for them. Used by the webhook handler to find the
    # local user when subscription events arrive. Added in alembic 033.
    stripe_customer_id = Column(String(64), unique=True, nullable=True, index=True)

    # Founding 100 flag. Aaron's pricing: founding members pay the SAME
    # $12/mo (NOT a discount) — the flag controls early-access + ecosystem
    # bundle eligibility (Project-Ops, Accounting-Ops, etc.). Granted
    # atomically at Stripe-checkout-completion time for annual-upfront
    # subscriptions when the cohort is open and below FOUNDING_100_LIMIT.
    # Renamed from `is_founder` in alembic 037 to make room for multiple
    # cohorts in the future (meeting_ops_v1, crisis_ops_v1, etc.).
    is_founding_member = Column(Boolean, nullable=False, default=False)

    # Founding cohort label (e.g. "meeting_ops_v1"). NULL when the user is
    # not a founding member. Allows future cohorts without a schema change
    # and supports the "how many seats left in cohort X" hot path. Added
    # in alembic 037.
    founding_cohort = Column(String(64), nullable=True)

    # Per-user "home" workspace (Organization). When set AND the user is
    # still a member of it, this is the top preference resolve_active_org()
    # falls back to when no X-MeetingOps-Org header / ?org= selector is
    # supplied — so a user's chosen workspace persists server-side across
    # devices + on any node. Replaces the dogfood-specific DEFAULT_ORG_SLUG
    # preference hack (which only helped the magic-unicorn node). Set via
    # PUT /api/organizations/default. NULL = no preference (fall through to
    # the existing fallback chain). FK is ON DELETE SET NULL so deleting an
    # org clears it rather than orphaning the user. Added in alembic 044.
    default_organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime)
    
    # Security
    failed_login_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    
    # API access
    api_key = Column(String, unique=True, nullable=True)
    api_key_created_at = Column(DateTime, nullable=True)
    
    # Relationships
    organizations = relationship("UserOrganization", back_populates="user", foreign_keys="UserOrganization.user_id")
    audit_logs = relationship("AuditLog", back_populates="user")
    personal_access_tokens = relationship(
        "PersonalAccessToken",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class PersonalAccessToken(Base):
    __tablename__ = "personal_access_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_personal_access_tokens_token_hash"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    token_hash = Column(String(64), nullable=False, index=True)
    token_prefix = Column(String(12), nullable=False)
    scope = Column(String(64), nullable=False, default="user")
    organization_id = Column(
        Integer,
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    expires_at = Column(DateTime(timezone=True), nullable=True)
    rotated_from_id = Column(
        Integer,
        ForeignKey("personal_access_tokens.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    revoked_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="personal_access_tokens")

class PasswordResetToken(Base):
    """Single-use password-reset tokens (v3.21.0).

    The user receives the plaintext (32-byte base64url) token via email.
    We store only the sha256 hex of that token here, so a DB compromise
    can't be used to reset accounts. Tokens expire 1 hour after issue
    (POST /api/auth/password/forgot) and are marked `used_at` on first
    successful reset (POST /api/auth/password/reset). The
    `ix_password_reset_tokens_token_hash` unique index makes lookup O(1).
    """

    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = Column(String(64), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class BetaInviteCode(Base):
    """Phase-1 beta invite codes (single-use by default).

    Self-serve signup is gated behind one of these when
    ``auth_config.REQUIRE_INVITE_CODE`` is on. Redeeming a code at
    /api/auth/register comps the new user's PERSONAL org to the Pro plan
    (mirrors api/stripe_webhook._apply_org_plan_from_sub: plan='pro',
    max_monthly_hours=None so services.quotas falls through to the Pro
    default). Admins mint codes (POST /api/admin/invite-codes); seed users
    hand them out. Phase 2 (NOT built here) lets an invitee mint their own.

    ``max_redemptions`` defaults to 1 (single-use). ``redemption_count`` is
    incremented ATOMICALLY at consume time (UPDATE ... WHERE id=:id AND
    redemption_count < max_redemptions) so two parallel registrations can't
    both burn the last seat. ``is_active`` is flipped False once the code is
    exhausted; an admin can also pre-disable a code by setting it False.

    ``created_by_user_id`` / ``redeemed_by_user_id`` are ON DELETE SET NULL
    so deleting a user neither orphans nor cascades a code (the audit value
    of "this code was redeemed" survives the redeemer's account deletion).
    """

    __tablename__ = "beta_invite_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(32), unique=True, index=True, nullable=False)
    created_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    max_redemptions = Column(Integer, nullable=False, default=1)
    redemption_count = Column(Integer, nullable=False, default=0)
    redeemed_by_user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    redeemed_at = Column(DateTime, nullable=True)
    # Invite-distribution idempotency stamp. Set once a code's invite email
    # has been successfully handed to Postmark; dry-runs and failed sends
    # leave it NULL so the operator can retry.
    emailed_at = Column(DateTime(timezone=True), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    created_by = relationship("User", foreign_keys=[created_by_user_id])
    redeemed_by = relationship("User", foreign_keys=[redeemed_by_user_id])


class UserOrganization(Base):
    __tablename__ = "user_organizations"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_user_organizations_user_org"),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    
    # Role in organization
    role = Column(String, nullable=False)  # admin, manager, user, viewer
    joined_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    invited_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # Permissions override (optional)
    custom_permissions = Column(get_json_type(), nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="organizations", foreign_keys=[user_id])
    organization = relationship("Organization", back_populates="users")
    invited_by_user = relationship("User", foreign_keys=[invited_by])

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True)
    
    # Action details
    action = Column(String, nullable=False)  # login, logout, create_session, delete_file, etc.
    resource_type = Column(String, nullable=True)  # session, file, user, etc.
    resource_id = Column(String, nullable=True)
    
    # Additional data
    ip_address = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    details = Column(get_json_type(), nullable=True)
    
    # Timestamp
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    user = relationship("User", back_populates="audit_logs")

# Note: Session organization support will be added later
