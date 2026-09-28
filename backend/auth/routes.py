import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from database.database import get_db
from auth.service import AuthService
from auth.dependencies import get_current_organization, get_current_user, require_admin, require_permission
from auth.email import send_password_reset_email, send_verification_email
from auth.models import Organization, PasswordResetToken, User, UserOrganization
from auth.config import auth_config
from auth.invite_codes import (
    comp_personal_org_to_pro,
    consume_invite_code,
    validate_invite_code,
)
from auth.tier import get_org_tier_features, get_tier_features, get_user_tier
from auth.utils import (
    generate_email_verification_token,
    get_password_hash,
    sanitize_email,
    validate_password,
    verify_email_verification_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["authentication"])

# Org/membership endpoints that read more naturally under /api/organizations
# than under the /api/auth prefix (the user's "home"/default workspace).
# Registered in main.py via _load_router(..., router_attr="organizations_router")
# so we don't proliferate a separate module — they live with the membership
# serialization helpers (_serialize_memberships) here.
organizations_router = APIRouter(prefix="/api/organizations", tags=["organizations"])

# Pydantic models for requests/responses
class UserCreate(BaseModel):
    email: EmailStr
    username: str
    password: str
    full_name: Optional[str] = None
    organization_id: Optional[int] = None
    role: str = "user"
    # Phase-1 beta gate (self-serve only). When auth_config.REQUIRE_INVITE_CODE
    # is on, a valid single-use code is required to register and comps the new
    # user's personal org to Pro. Ignored on the admin-create path.
    invite_code: Optional[str] = None
    
    @field_validator('username')
    @classmethod
    def username_alphanumeric(cls, v: str):
        if not v.replace('_', '').isalnum():
            raise ValueError('Username must be alphanumeric')
        if len(v) < 3 or len(v) > 30:
            raise ValueError('Username must be between 3 and 30 characters')
        return v.lower()

class UserResponse(BaseModel):
    id: int
    email: str
    username: str
    full_name: Optional[str]
    is_active: bool
    is_verified: bool
    is_superuser: bool = False
    created_at: datetime
    role: Optional[str] = None  # Will be populated from organization role or superuser status
    organizations: list[dict] = []
    active_organization: Optional[dict] = None
    # Tier-based feature gating. `tier` is the resolved effective tier
    # (is_superuser overrides to 'enterprise'); `tier_features` is the
    # full capability dict from TIER_FEATURES so the frontend can render
    # without a follow-up round trip. See backend/auth/tier.py.
    tier: str = "free"
    tier_features: dict[str, Any] = {}
    # billing-1: the ACTIVE workspace's feature dict (from its plan). The
    # frontend renders the EFFECTIVE capability = AND(tier_features,
    # active_org_features) so a paid user in an unpaid workspace sees gated UI
    # instead of an enabled control the backend then 403s. Empty when there's
    # no active org (no restriction). Mirrors auth.tier.get_org_tier_features.
    active_org_features: dict[str, Any] = {}
    # Stripe billing surface — populated so the frontend can render the
    # "Founding 100" badge + the right Pricing-page CTA on the very first
    # render after login. `founding_cohort` is the cohort label (e.g.
    # "meeting_ops_v1") or None when the user is not a founding member.
    is_founding_member: bool = False
    founding_cohort: Optional[str] = None
    has_stripe_customer: bool = False

    model_config = ConfigDict(from_attributes=True)

class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    department: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None

class PasswordChange(BaseModel):
    old_password: str
    new_password: str

class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse

class LoginRequest(BaseModel):
    username: str  # Can be username or email
    password: str

class RefreshRequest(BaseModel):
    refresh_token: str


class OrganizationMembershipResponse(BaseModel):
    id: int
    name: str
    slug: str
    role: str
    is_active: bool
    # billing-1: the workspace's plan (free/pro/enterprise). The backend gate is
    # authoritative per active workspace, so the frontend needs this to render
    # the EFFECTIVE capability = min(user tier, active-org plan) instead of
    # showing enabled UI that the backend then 403s. Already loaded on the ORM
    # row — no extra query.
    plan: str = "free"


class SetDefaultOrganizationRequest(BaseModel):
    organization_id: int


def _serialize_memberships(user: User) -> list[dict]:
    memberships: list[dict] = []
    for membership in user.organizations:
        organization = membership.organization
        if not organization:
            continue
        memberships.append(
            OrganizationMembershipResponse(
                id=organization.id,
                name=organization.name,
                slug=organization.slug,
                role=membership.role,
                is_active=organization.is_active,
                plan=(organization.plan or "free"),
            ).model_dump()
        )

    memberships.sort(key=lambda item: item["name"].lower())
    return memberships


def _build_user_response(
    user: User,
    *,
    active_org: Optional[Organization] = None,
    active_role: Optional[str] = None,
) -> UserResponse:
    organizations = _serialize_memberships(user)
    role = "superuser" if user.is_superuser else active_role
    if not role and organizations:
        role = organizations[0]["role"]
    if not role:
        role = "user"

    active_organization = None
    if active_org:
        for org in organizations:
            if org["id"] == active_org.id:
                active_organization = org
                break
        if active_organization is None:
            active_organization = {
                "id": active_org.id,
                "name": active_org.name,
                "slug": active_org.slug,
                "role": active_role or ("superuser" if user.is_superuser else "viewer"),
                "is_active": active_org.is_active,
                "plan": (active_org.plan or "free"),
            }

    return UserResponse(
        id=user.id,
        email=user.email,
        username=user.username,
        full_name=user.full_name,
        is_active=user.is_active,
        is_verified=user.is_verified,
        is_superuser=user.is_superuser,
        created_at=user.created_at,
        role=role,
        organizations=organizations,
        active_organization=active_organization,
        tier=get_user_tier(user),
        tier_features=get_tier_features(user),
        active_org_features=(get_org_tier_features(active_org) if active_org else {}),
        is_founding_member=bool(getattr(user, "is_founding_member", False)),
        founding_cohort=getattr(user, "founding_cohort", None),
        has_stripe_customer=bool(getattr(user, "stripe_customer_id", None)),
    )

# Routes
from auth.dependencies import get_current_user_optional


async def _optional_caller_for_register(
    caller: Optional[User] = Depends(get_current_user_optional),
) -> Optional[User]:
    """Self-serve signup dependency.

    `register()` accepts either an authenticated admin (legacy invite-only
    path) OR an anonymous caller (self-serve when ALLOW_REGISTRATION=true).
    The actual access check happens inside `register()` against
    `auth_config.ALLOW_REGISTRATION`.

    Replaces the v3.2.0 inline ternary `Depends(require_admin) if not
    auth_config.ALLOW_REGISTRATION else None` which FastAPI/Pydantic
    rejected at module import when ALLOW_REGISTRATION=true (the `None`
    branch left `current_user: Optional[User]` with no Depends and
    Pydantic tried to treat User as a response field).
    """
    return caller


@router.post("/register", response_model=UserResponse)
async def register(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(_optional_caller_for_register),
):
    """Register a new user"""
    # Check if registration is allowed
    if not auth_config.ALLOW_REGISTRATION and not current_user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Registration is disabled. Contact an administrator."
        )
    
    # Self-serve = no admin caller. Self-serve consumers get a private
    # personal org (not the shared default) and an email-verification link;
    # admin-created users keep the legacy behavior.
    is_self_serve = current_user is None

    # Phase-1 beta gate (SELF-SERVE ONLY). A valid single-use code, when
    # redeemed, comps the new user to a TIME-LIMITED Pro tier on BOTH gated
    # surfaces (user.tier + personal-org plan) via comp_personal_org_to_pro.
    # Two self-serve shapes:
    #   - REQUIRE_INVITE_CODE on  -> a code is REQUIRED. Validate always; an
    #     empty/absent code 403s (validate_invite_code raises on blank).
    #   - REQUIRE_INVITE_CODE off -> a code is OPTIONAL. If the user supplies
    #     one we validate + honor it (optional code -> Pro comp); a blank/absent
    #     code stays a free public signup (no validation, no comp).
    # Validate BEFORE create_user so a bad code never creates a user. The
    # admin-create path (current_user set) is always exempt.
    invite_row = None
    supplied_code = (user_data.invite_code or "").strip()
    code_required = is_self_serve and auth_config.REQUIRE_INVITE_CODE
    if is_self_serve and (code_required or supplied_code):
        try:
            invite_row = validate_invite_code(db, user_data.invite_code)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            )

    try:
        user = AuthService.create_user(
            db,
            email=user_data.email,
            username=user_data.username,
            password=user_data.password,
            full_name=user_data.full_name,
            organization_id=user_data.organization_id,
            role=user_data.role,
            created_by_id=current_user.id if current_user else None,
            personal_org=is_self_serve and user_data.organization_id is None,
        )
        # Consume the code only AFTER create_user succeeds (a failed create
        # raises above and must not burn a code). The consume is atomic
        # (UPDATE ... WHERE redemption_count < max_redemptions); if a parallel
        # registration claimed the last seat between validate + consume, this
        # loses the race -> 403, and the just-created user is rolled back so a
        # raced signup doesn't slip through ungated.
        if invite_row is not None:
            claimed = consume_invite_code(db, invite_row.id, user.id)
            if not claimed:
                # Lost the race for the last seat. Roll back the just-created
                # user (and its FK refs) so a raced signup can't slip through
                # ungated, then 403 so the client can retry with another code.
                _delete_user_fk_references(db, user.id)
                db.delete(user)
                db.commit()
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="A valid invite code is required.",
                )
            # Comp the new user to a TIME-LIMITED Pro tier on BOTH gated
            # surfaces: user.tier='pro' + tier_expires_at=now+N days AND the
            # personal org plan='pro' (mirrors the Stripe webhook). Setting
            # only ONE still 403'd paid server-compute (billing-1); the expiry
            # lets session_watchdog auto-revert an unpaid comp so a "free
            # month" can't become permanent. `days` defaults to
            # auth.invite_codes.DEFAULT_COMP_DAYS (BetaInviteCode carries no
            # per-code duration column). Best-effort on the org side.
            comp_personal_org_to_pro(db, user)
        if is_self_serve and not user.is_verified:
            # Best-effort: a mail failure must not fail signup (the user can
            # resend). send_verification_email logs + returns False if SMTP
            # is unconfigured.
            token = generate_email_verification_token(user.email)
            send_verification_email(
                user.email,
                f"{auth_config.APP_BASE_URL}/api/auth/verify-email?token={token}",
            )
        # Founding 100 grant happens at Stripe-checkout-completion time
        # (annual-upfront subscriptions only), not at signup. The grant
        # is wired in api/stripe_webhook.py._maybe_grant_founding_member;
        # it intentionally does NOT fire here so free-tier signups don't
        # consume a cohort seat.
        return _build_user_response(user)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

class ResendVerificationRequest(BaseModel):
    email: EmailStr


@router.get("/verify-email")
async def verify_email(token: str, db: Session = Depends(get_db)):
    """Confirm an email-verification link, then redirect to the app login
    with a status flag. Idempotent: re-clicking a still-valid link lands on
    success again. Invalid/expired tokens redirect to an error state rather
    than exposing whether the email exists."""
    redirect_ok = f"{auth_config.APP_BASE_URL}/#/login?verify=success"
    redirect_bad = f"{auth_config.APP_BASE_URL}/#/login?verify=invalid"
    email = verify_email_verification_token(token)
    if not email:
        return RedirectResponse(redirect_bad, status_code=303)
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return RedirectResponse(redirect_bad, status_code=303)
    if not user.is_verified:
        user.is_verified = True
        db.commit()
        AuthService.log_action(db, user.id, "email_verified", "user", str(user.id))
    return RedirectResponse(redirect_ok, status_code=303)


@router.post("/resend-verification")
async def resend_verification(
    payload: ResendVerificationRequest,
    db: Session = Depends(get_db),
):
    """Re-send a verification email. Always returns the same 200 message so
    it can't be used to enumerate which addresses have accounts."""
    try:
        email = sanitize_email(payload.email)
    except ValueError:
        email = None
    if email:
        user = db.query(User).filter(User.email == email).first()
        if user and not user.is_verified:
            token = generate_email_verification_token(user.email)
            send_verification_email(
                user.email,
                f"{auth_config.APP_BASE_URL}/api/auth/verify-email?token={token}",
            )
    return {
        "message": "If an unverified account exists for that address, a verification link has been sent."
    }


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db)
):
    """Login with username/email and password"""
    user = AuthService.authenticate_user(db, form_data.username, form_data.password)
    
    if not user:
        # Log failed login attempt
        AuthService.log_action(
            db, None, "login_failed",
            ip_address=request.client.host if request.client else None,
            details={"username": form_data.username}
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Create tokens
    tokens = AuthService.create_tokens(user)
    
    default_org = None
    default_role = None
    if user.organizations:
        default_org = user.organizations[0].organization
        default_role = user.organizations[0].role
    
    return TokenResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        user=_build_user_response(
            user,
            active_org=default_org,
            active_role=default_role,
        )
    )

@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: RefreshRequest,
    db: Session = Depends(get_db)
):
    """Refresh access token using refresh token"""
    from auth.utils import decode_token

    # Decode refresh token
    payload = decode_token(request.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )
    
    # Get user
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token"
        )
    
    user = AuthService.get_user_by_id(db, int(user_id))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive"
        )
    
    # Create new tokens
    tokens = AuthService.create_tokens(user, payload.get("org_id"))
    
    default_org = None
    default_role = None
    if user.organizations:
        default_org = user.organizations[0].organization
        default_role = user.organizations[0].role
    
    return TokenResponse(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        user=_build_user_response(
            user,
            active_org=default_org,
            active_role=default_role,
        )
    )

@router.get("/me", response_model=UserResponse)
async def get_current_user_profile(
    current_user: User = Depends(get_current_user),
    active_org=Depends(get_current_organization),
):
    """Get current user profile"""
    return _build_user_response(
        current_user,
        active_org=active_org.organization,
        active_role=active_org.role,
    )

@router.put("/me", response_model=UserResponse)
async def update_current_user_profile(
    user_update: UserUpdate,
    current_user: User = Depends(get_current_user),
    active_org=Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Update current user profile"""
    updated_user = AuthService.update_user(
        db,
        current_user.id,
        full_name=user_update.full_name,
        department=user_update.department,
        phone=user_update.phone,
        avatar_url=user_update.avatar_url
    )
    return _build_user_response(
        updated_user,
        active_org=active_org.organization,
        active_role=active_org.role,
    )

@router.post("/me/change-password")
async def change_password(
    password_data: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Change current user password"""
    success = AuthService.change_password(
        db,
        current_user.id,
        password_data.old_password,
        password_data.new_password
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid old password"
        )
    
    return {"message": "Password changed successfully"}

@router.post("/me/api-key")
async def generate_api_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate a new API key for current user"""
    api_key = AuthService.generate_user_api_key(db, current_user.id)
    return {
        "api_key": api_key,
        "message": "API key generated successfully. Store it securely as it won't be shown again."
    }


@router.get("/orgs", response_model=list[OrganizationMembershipResponse])
async def list_current_user_orgs(
    current_user: User = Depends(get_current_user),
):
    """List the authenticated user's organizations."""
    return [
        OrganizationMembershipResponse(**membership)
        for membership in _serialize_memberships(current_user)
    ]


def _delete_user_fk_references(db: Session, user_id: int) -> None:
    """Remove or null every row that references ``users.id`` WITHOUT a delete
    cascade, so the final ``db.delete(user)`` can flush cleanly.

    Verified against auth/models.py + database/models.py + the live alembic
    migrations (everything else FKing users.id is ondelete=CASCADE/SET NULL):

      - ``UserOrganization.user_id`` (NOT NULL, no ondelete): the User
        relationship has no delete cascade, so SQLAlchemy tries to NULL the
        FK at flush -> IntegrityError on EVERY user delete. Bulk-delete the
        memberships instead.
      - ``UserOrganization.invited_by`` (nullable, no ondelete, no reverse
        relationship): Postgres rejects the user delete while any membership
        still names them as inviter. Null it out — invitees keep their seat.
      - ``UploadJob.user_id`` / ``TtsJob.user_id`` (NOT NULL, no ondelete, no
        ORM relationship): Postgres-only aborts. Session-bound jobs die with
        the session cascade, but session-less upload jobs and TTS jobs the
        user queued on OTHER people's shared sessions survive -> delete by
        user_id explicitly.

    AuditLog.user_id is intentionally untouched: it is nullable and the
    User.audit_logs relationship nullifies it at flush, preserving the trail.
    """
    from database.models import TtsJob, UploadJob

    db.query(UploadJob).filter(UploadJob.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(TtsJob).filter(TtsJob.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(UserOrganization).filter(
        UserOrganization.user_id == user_id
    ).delete(synchronize_session=False)
    db.query(UserOrganization).filter(
        UserOrganization.invited_by == user_id
    ).update({UserOrganization.invited_by: None}, synchronize_session=False)


@router.delete("/me")
async def delete_my_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Self-serve account deletion (App Review 5.1.1(v)).

    The iOS app calls this on the cloud Delete-Account path. The admin
    `DELETE /users/{id}` refuses self-deletion, so this is the self-serve path.

    WHAT IS DELETED:
      - DB rows, in FK-safe order: the caller's Transcriptions (plain-Integer
        session ref, no FK) + RecordingSessions (children — action_items,
        session_collaborators, session-bound upload/tts jobs, chunk embeddings
        — cascade at the DB level per migration 012), their UploadJobs +
        TtsJobs, their UserOrganization memberships, then the User row itself
        (ORM-cascades personal_access_tokens; password_reset_tokens +
        user_voice_profile + bulk-import jobs cascade at the DB level).
        Memberships this user *invited* are kept with invited_by nulled;
        audit logs are kept with user_id nulled.
      - AFTER the DB commit, best-effort per deleted session: Qdrant vectors
        (semantic_search.delete_session) and Garage/local media objects
        (media_storage.delete_prefix on '{org}/{session_id}/'). Failures are
        logged and never surfaced — the DB rows are already gone, so the
        request still returns 200.
      - AFTER the DB commit, best-effort: the user's KEYCLOAK identity
        (services.keycloak_admin.delete_keycloak_user, keyed by email), but
        ONLY when the KC admin client env is configured (KEYCLOAK_ADMIN_BASE
        or KEYCLOAK_URL + KEYCLOAK_ADMIN_CLIENT_ID/SECRET). When it is, this
        is full right-to-erasure: the SSO login is gone too. When it is NOT
        (self-host without SSO, or admin client not yet provisioned), the
        Keycloak identity SURVIVES and re-authenticating auto-provisions a
        fresh, EMPTY account — data erasure is still complete, login closure
        is not. KC failures are logged and never fail the request.

    NOT DELETED (known limitations):
      - Org-level SpeakerProfiles / voice samples (org data, possibly shared;
        created_by/linked_user FKs are SET NULL).
    """
    from database.models import RecordingSession, Transcription

    uid = current_user.id
    # Captured BEFORE the delete/commit (the ORM instance is gone after):
    # the post-commit Keycloak identity erasure needs the email.
    user_email = current_user.email
    AuthService.log_action(
        db, uid, "account_self_deleted",
        details={"username": current_user.username},
    )

    # Capture (pk, org_id, canonical string id) BEFORE deleting the rows —
    # the post-commit Qdrant/media purge needs them and they're gone after.
    session_rows = (
        db.query(
            RecordingSession.id,
            RecordingSession.organization_id,
            RecordingSession.session_id,
            RecordingSession.brigade_graph_node_id,
        )
        .filter(RecordingSession.user_id == uid)
        .all()
    )
    session_pks = [row.id for row in session_rows]
    session_refs = [
        (row.organization_id, row.session_id or str(row.id), row.brigade_graph_node_id)
        for row in session_rows
    ]

    if session_pks:
        # No FK cascade on transcriptions -> delete by session pk first.
        db.query(Transcription).filter(
            Transcription.session_id.in_(session_pks)
        ).delete(synchronize_session=False)
        # chat_history has no FK to recording_sessions (plain string
        # session_key), so it isn't cascaded — purge the per-meeting AI chat
        # for every deleted session so transcript-derived PII doesn't survive
        # account deletion (mirrors _delete_session_record).
        #
        # SECURITY: session_key is NOT globally unique — legacy sessions use the
        # integer pk as the canonical key, so deleting by session_key alone can
        # wipe another org's chat history on a key collision (cross-tenant
        # destructive write). Pair each key with its org and delete per org.
        from collections import defaultdict
        from database.models import ChatHistory as _ChatHistory
        keys_by_org: dict[int, list[str]] = defaultdict(list)
        for (org_id, cid, _node) in session_refs:
            keys_by_org[org_id].append(cid)
        for org_id, keys in keys_by_org.items():
            db.query(_ChatHistory).filter(
                _ChatHistory.organization_id == org_id,
                _ChatHistory.session_key.in_(keys),
            ).delete(synchronize_session=False)
        # Deleting the sessions DB-cascades action_items, session_collaborators,
        # session-bound upload/tts jobs, chunk embeddings, attachments, ...
        db.query(RecordingSession).filter(
            RecordingSession.user_id == uid
        ).delete(synchronize_session=False)

    # Rows that would otherwise abort the user delete on FK integrity
    # (memberships, invited_by, session-less upload jobs, TTS jobs on
    # other people's sessions).
    _delete_user_fk_references(db, uid)

    db.delete(current_user)  # ORM-cascades PATs; nullifies audit_logs.user_id
    db.commit()

    # Post-commit, best-effort external purge. Mirrors the canonical
    # single-session delete (api/recording.py discard_always_on_session):
    # import-on-use, swallow + log failures, never fail the request — the
    # DB delete above is the authoritative "account is gone" signal.
    for org_id, canonical_id, brigade_node_id in session_refs:
        try:
            from services.semantic_search_service import semantic_search

            semantic_search.delete_session(canonical_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "account-delete: Qdrant cleanup failed session=%s: %s",
                canonical_id, exc,
            )
        if brigade_node_id:
            try:
                from services.brigade_writer import delete_session_from_brigade
                result = await delete_session_from_brigade(org_id, brigade_node_id)
                if not result.ok:
                    logger.warning(
                        "account-delete: Brigade cleanup failed session=%s: %s",
                        canonical_id, result.detail,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "account-delete: Brigade cleanup failed session=%s: %s",
                    canonical_id, exc,
                )
        try:
            from services import media_storage

            media_storage.delete_prefix(prefix=f"{org_id}/{canonical_id}/")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "account-delete: media cleanup failed session=%s: %s",
                canonical_id, exc,
            )

    # Best-effort SSO identity erasure (right-to-erasure): without this the
    # Keycloak identity survives and the next OIDC login silently
    # re-provisions a fresh empty account. Config-gated (no-ops unless the
    # KC admin client env is set) and fail-open — the DB delete above is
    # already committed, so a KC failure must never fail the request; the
    # log line lets an operator finish the erasure by hand.
    try:
        from services.keycloak_admin import delete_keycloak_user

        kc_deleted = await delete_keycloak_user(user_email)
        logger.info(
            "account-delete: Keycloak identity for %s: %s",
            user_email,
            "deleted" if kc_deleted else
            "NOT deleted (admin client unconfigured, local-only account, or KC error — see keycloak-admin logs)",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "account-delete: Keycloak identity delete failed for %s: %s",
            user_email, exc,
        )

    return {"message": "Account deleted"}


@organizations_router.put("/default", response_model=UserResponse)
async def set_default_organization(
    payload: SetDefaultOrganizationRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set the caller's "home"/default workspace (Organization).

    The default org is the top preference that
    `auth.organization.resolve_active_organization` falls back to when a
    request carries no `X-MeetingOps-Org` header / `?org=` selector — so a
    user's chosen workspace persists server-side across devices + on any
    node. The caller MUST be a member of the target org (or a superuser);
    we never let a user pin a workspace they can't access.
    """
    organization = (
        db.query(Organization)
        .filter(Organization.id == payload.organization_id)
        .first()
    )
    if not organization:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

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

    current_user.default_organization_id = organization.id
    db.add(current_user)
    db.commit()
    db.refresh(current_user)

    return _build_user_response(
        current_user,
        active_org=organization,
        active_role=(membership.role if membership else None),
    )

@router.post("/logout")
async def logout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Logout current user"""
    # Log logout action
    AuthService.log_action(db, current_user.id, "logout", "user", str(current_user.id))
    
    # In a stateless JWT system, logout is handled client-side
    # We just log the action and return success
    return {"message": "Logged out successfully"}


@router.get("/sso-logout")
async def sso_logout(request: Request):
    """Full SSO logout — actually ends the Keycloak session, not just cookies.

    The frontend (AuthContext) full-page-navigates here on logout. Two deploy
    shapes are handled, and both must END the KC SSO session — otherwise KC keeps
    it alive and the SPA's zero-click SSO silently re-authenticates the user
    straight back in ("authenticating…" then right back in):

    1. oauth2-proxy-fronted (dogfood): the proxy injects the user's Keycloak ID
       token as the Authorization header (SET_AUTHORIZATION_HEADER=true). We pass
       it as ``id_token_hint``; issuer/client/redirect are all derived from the
       token + request, so it's env-agnostic, and we bounce through the proxy's
       /oauth2/sign_out last so the proxy session cookie is cleared too.
    2. native-OIDC (prod, auth/oidc_sso.py): no proxy, no Authorization header.
       The session is the HttpOnly mo_uc_session cookie and the id_token is in
       mo_uc_idt. We redirect straight to KC's end-session endpoint with the
       stored id_token_hint + client_id and clear both cookies.
    """
    import base64
    import json as _json
    from urllib.parse import quote

    auth_header = request.headers.get("authorization", "")
    id_token = auth_header[7:].strip() if auth_header[:7].lower() == "bearer " else ""

    # Public origin from the fronting proxy headers (env-aware — no hardcoding).
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    post_logout = f"{proto}://{host}/"

    issuer = None
    client_id = None
    if id_token.count(".") >= 2:
        try:
            payload = id_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)  # restore base64 padding
            claims = _json.loads(base64.urlsafe_b64decode(payload))
            issuer = claims.get("iss")
            aud = claims.get("aud")
            client_id = claims.get("azp") or (aud if isinstance(aud, str) else None)
        except Exception:  # noqa: BLE001 — best-effort decode; fall back below
            pass

    if issuer:
        # Send BOTH id_token_hint and client_id. id_token_hint skips Keycloak's
        # "Do you want to log out?" confirmation page — the automated redirect
        # can't click it, so without it the logout dead-ends there (oauth2-proxy
        # keeps the ID token fresh enough that KC accepts it). KC then ends the
        # uchub session AND — because the "Unicorn Commander" broker IdP is set to
        # FRONT-CHANNEL logout (backchannelSupported=false) — redirects the browser
        # through the upstream UC logout too, clearing that session (else the
        # upstream silently re-brokers you back in). iss + client_id are derived
        # from the token, so this stays environment-agnostic.
        kc = (
            f"{issuer}/protocol/openid-connect/logout"
            f"?post_logout_redirect_uri={quote(post_logout, safe='')}"
        )
        if id_token:
            kc += f"&id_token_hint={quote(id_token, safe='')}"
        if client_id:
            kc += f"&client_id={quote(client_id, safe='')}"
        target = f"/oauth2/sign_out?rd={quote(kc, safe='')}"
        return RedirectResponse(url=target, status_code=302)

    # No Authorization header => not behind oauth2-proxy. The prod/native-OIDC
    # path (auth/oidc_sso.py) authenticates via the HttpOnly mo_uc_session cookie
    # and stashes the KC id_token in mo_uc_idt. If the session cookie is present,
    # end the Keycloak SSO session directly (no proxy to bounce through) and clear
    # both cookies. id_token_hint skips KC's "Do you want to log out?" interstitial;
    # without it (sessions predating the idt cookie) KC shows a one-click confirm.
    from auth.oidc_sso import (
        KC_ISSUER, KC_CLIENT_ID, APP_PUBLIC_URL,
        SESSION_COOKIE_NAME, IDT_COOKIE_NAME, IDT_COOKIE_PATH,
    )
    if request.cookies.get(SESSION_COOKIE_NAME):
        stored_idt = request.cookies.get(IDT_COOKIE_NAME) or ""
        post_logout_q = quote(f"{APP_PUBLIC_URL}/", safe="")
        kc = (
            f"{KC_ISSUER}/protocol/openid-connect/logout"
            f"?post_logout_redirect_uri={post_logout_q}"
            f"&client_id={quote(KC_CLIENT_ID, safe='')}"
        )
        if stored_idt:
            kc += f"&id_token_hint={quote(stored_idt, safe='')}"
        resp = RedirectResponse(url=kc, status_code=302)
        resp.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
        resp.delete_cookie(IDT_COOKIE_NAME, path=IDT_COOKIE_PATH, secure=True, httponly=True, samesite="lax")
        return resp

    # Truly unauthenticated (local dev, or already logged out): the SPA cleared
    # its local token before redirecting here, so just send it home → /login.
    return RedirectResponse(url="/", status_code=302)

# Admin routes
@router.get("/users", response_model=list[UserResponse])
async def list_users(
    skip: int = 0,
    limit: int = 100,
    organization_id: Optional[int] = None,
    current_user: User = Depends(require_permission("user.read")),
    active_org=Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """List all users (admin only)"""
    if organization_id and organization_id != active_org.organization.id and not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to that organization",
        )

    target_org_id = organization_id or active_org.organization.id
    query = (
        db.query(User)
        .join(User.organizations)
        .filter(UserOrganization.organization_id == target_org_id)
    )
    users = query.offset(skip).limit(limit).all()
    target_org = db.query(Organization).filter(Organization.id == target_org_id).first()
    return [
        _build_user_response(
            user,
            active_org=target_org,
            active_role=AuthService.get_user_role(db, user.id, target_org_id),
        )
        for user in users
    ]

@router.put("/users/{user_id}/activate")
async def activate_user(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Activate a user account"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    user.is_active = True
    user.is_verified = True
    db.commit()
    
    return {"message": "User activated successfully"}

@router.put("/users/{user_id}/deactivate")
async def deactivate_user(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Deactivate a user account"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account"
        )
    
    user.is_active = False
    db.commit()
    
    return {"message": "User deactivated successfully"}

@router.put("/users/{user_id}/role")
async def update_user_role(
    user_id: int,
    role_data: dict,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Update a user's role"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    new_role = role_data.get("role")
    if new_role not in ["user", "manager", "admin", "superuser", "viewer"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid role"
        )
    
    # Update role by setting superuser status
    if new_role == "superuser":
        user.is_superuser = True
    else:
        user.is_superuser = False
    
    db.commit()
    
    # Log the action
    AuthService.log_action(
        db, current_user.id, "role_update", 
        details={"user_id": user_id, "new_role": new_role}
    )
    
    return {"message": f"User role updated to {new_role}"}

@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db)
):
    """Admin: delete a user account.

    Deletes the User row plus every row that would otherwise block it on FK
    integrity (org memberships, invited_by backrefs, upload/TTS jobs — see
    `_delete_user_fk_references`; without that, this endpoint 500'd with an
    IntegrityError for ANY user with an org membership, i.e. all of them).
    PATs cascade via the ORM; audit logs are kept with user_id nulled.

    Intentionally does NOT touch the user's RecordingSessions/Transcriptions
    (org data the admin may want to keep — recording_sessions.user_id is a
    plain Integer with no FK, so the rows survive as org-scoped history).
    For full data removal the user should call DELETE /api/auth/me, which
    also purges sessions, Qdrant vectors and media objects.

    POLICY — the Keycloak identity is intentionally NOT deleted here: an
    org admin removing a user from THIS app must not destroy the human's
    suite-wide SSO login (the same uchub identity may back other UC apps and
    other orgs). If the user re-authenticates they get a fresh, empty
    Meeting-Ops account. Identity erasure belongs to the account owner via
    the self-serve DELETE /api/auth/me (which deletes the KC user when the
    admin client env is configured — see services.keycloak_admin).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account"
        )

    # Log the action before deletion
    AuthService.log_action(
        db, current_user.id, "user_deleted",
        details={"deleted_user_id": user_id, "deleted_username": user.username}
    )

    # Rows that would otherwise abort the delete on FK integrity.
    _delete_user_fk_references(db, user_id)

    db.delete(user)
    db.commit()

    return {"message": "User deleted successfully"}


# ---------------------------------------------------------------------------
# Password reset (v3.21.0)
# ---------------------------------------------------------------------------

_password_reset_logger = logging.getLogger(__name__ + ".password_reset")

# Rate limit: max 3 forgot-password requests per email per hour.
_PASSWORD_RESET_RATE_LIMIT_MAX = 3
_PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS = 60 * 60
_PASSWORD_RESET_TOKEN_TTL_HOURS = 1
_PASSWORD_RESET_RL_INPROC: dict[str, list[float]] = {}


def _hash_reset_token(plaintext: str) -> str:
    """sha256 hex of the plaintext token. Used both at issue time
    (what we persist) and at validation time (what we look up by)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _public_url() -> str:
    """Base URL the password-reset link points at. Mirrors billing._app_base_url:
    APP_BASE_URL > FRONTEND_BASE_URL > localhost fallback."""
    raw = (
        os.getenv("APP_BASE_URL")
        or os.getenv("FRONTEND_BASE_URL")
        or "http://localhost:7777"
    ).rstrip("/")
    return raw


async def _password_reset_under_rate_limit(email_key: str) -> bool:
    """3-per-hour per-email rate limit. Redis-first (SETEX counter), with
    an in-proc fallback that matches the landing-page pattern. The key
    space is namespaced so it doesn't collide with the landing limiter.
    """
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            import redis.asyncio as redis  # local import
            client = await redis.from_url(redis_url, decode_responses=True)
            try:
                key = f"auth:pwreset:rl:{email_key}"
                count = await client.incr(key)
                if count == 1:
                    await client.expire(
                        key, _PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS
                    )
                return count <= _PASSWORD_RESET_RATE_LIMIT_MAX
            finally:
                await client.close()
        except Exception as exc:  # noqa: BLE001
            _password_reset_logger.warning(
                "password-reset rate-limit: redis unavailable (%s); falling back to in-proc",
                exc,
            )

    # In-proc fallback (per-pod, not cross-pod safe — same caveat as landing).
    import time as _time
    now = _time.time()
    bucket = [
        t for t in _PASSWORD_RESET_RL_INPROC.get(email_key, [])
        if now - t < _PASSWORD_RESET_RATE_LIMIT_WINDOW_SECONDS
    ]
    if len(bucket) >= _PASSWORD_RESET_RATE_LIMIT_MAX:
        _PASSWORD_RESET_RL_INPROC[email_key] = bucket
        return False
    bucket.append(now)
    _PASSWORD_RESET_RL_INPROC[email_key] = bucket
    return True


class PasswordResetForgotRequest(BaseModel):
    email: EmailStr


class PasswordResetSubmitRequest(BaseModel):
    token: str
    new_password: str


@router.post(
    "/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
)
async def password_reset_forgot(
    payload: PasswordResetForgotRequest,
    db: Session = Depends(get_db),
):
    """Initiate a password reset.

    Always returns 202 with the same body regardless of whether the
    email matches a user — never leak account existence. When the email
    matches an active user, we generate a single-use token, persist its
    sha256, and email the plaintext link to the user. Rate limited to 3
    requests per email per hour (Redis SETEX with in-proc fallback).
    """
    response_body = {
        "message": (
            "If an account matches that email, a reset link is on the way."
        )
    }

    try:
        email = sanitize_email(payload.email)
    except ValueError:
        email = None
    if not email:
        return response_body

    # Rate-limit on the normalized email so attackers can't bypass by
    # case-perturbing the address.
    if not await _password_reset_under_rate_limit(email):
        _password_reset_logger.info(
            "password-reset: rate-limited email=%s", email
        )
        # Still return 202 — rate-limit existence is also a leak vector.
        return response_body

    user = db.query(User).filter(User.email == email).first()
    if user is None or not user.is_active:
        return response_body

    # Cryptographically random 32-byte token, base64url encoded
    # (token_urlsafe(32) returns ~43 chars).
    plaintext = secrets.token_urlsafe(32)
    token_hash = _hash_reset_token(plaintext)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=_PASSWORD_RESET_TOKEN_TTL_HOURS)

    reset = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=expires_at,
        created_at=now,
    )
    db.add(reset)
    db.commit()

    reset_url = f"{_public_url()}/reset-password?token={plaintext}"
    # send_password_reset_email is best-effort + non-raising.
    send_password_reset_email(user.email, reset_url)
    _password_reset_logger.info(
        "password-reset: token issued user_id=%s expires_at=%s",
        user.id, expires_at.isoformat(),
    )
    return response_body


def _lookup_reset_token(db: Session, token: str) -> Optional[PasswordResetToken]:
    """Resolve a plaintext token to its persisted row by sha256 hash.
    Returns None when the token doesn't exist."""
    if not token:
        return None
    token_hash = _hash_reset_token(token)
    return (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == token_hash)
        .first()
    )


def _reset_token_state(record: Optional[PasswordResetToken]) -> str:
    """Return one of 'missing' / 'used' / 'expired' / 'valid'."""
    if record is None:
        return "missing"
    if record.used_at is not None:
        return "used"
    expires_at = record.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        # SQLite returns naive datetimes; treat as UTC.
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at is not None and expires_at < datetime.now(timezone.utc):
        return "expired"
    return "valid"


@router.get("/password/reset/validate")
async def password_reset_validate(
    token: str,
    db: Session = Depends(get_db),
):
    """Lightweight check that a reset token is still good before showing
    the new-password form. 200 on valid; 410 on expired/used; 400 on
    unknown/malformed."""
    record = _lookup_reset_token(db, token)
    state = _reset_token_state(record)
    if state == "valid":
        return {"valid": True}
    if state in ("expired", "used"):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This password-reset link has expired.",
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This password-reset link is not valid.",
    )


@router.post("/password/reset")
async def password_reset_submit(
    payload: PasswordResetSubmitRequest,
    db: Session = Depends(get_db),
):
    """Complete a password reset.

    Validates the token (lookup by sha256 hash, not used, not expired),
    enforces the existing password policy, updates the user's
    hashed_password, and marks the token used. Returns 200 on success,
    400 on invalid/malformed token or weak password, 410 on expired/used
    token."""
    record = _lookup_reset_token(db, payload.token)
    state = _reset_token_state(record)
    if state in ("expired", "used"):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This password-reset link has expired.",
        )
    if state != "valid" or record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This password-reset link is not valid.",
        )

    ok, errors = validate_password(payload.new_password)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=" ".join(errors) or "Password does not meet policy.",
        )

    user = db.query(User).filter(User.id == record.user_id).first()
    if user is None or not user.is_active:
        # Same response shape as expired — don't leak whether the user
        # was deleted between issue + submit.
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This password-reset link is no longer valid.",
        )

    user.hashed_password = get_password_hash(payload.new_password)
    record.used_at = datetime.now(timezone.utc)
    db.add(user)
    db.add(record)
    db.commit()
    AuthService.log_action(
        db, user.id, "password_reset_completed",
        resource_type="user", resource_id=str(user.id),
    )
    _password_reset_logger.info(
        "password-reset: completed user_id=%s", user.id
    )
    return {"message": "Password updated. You can sign in with the new password."}
