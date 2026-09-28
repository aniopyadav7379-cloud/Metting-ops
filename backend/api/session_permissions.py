"""Per-meeting collaborator permissions API.

Implements the per-session leaf of the RBAC plan in RBAC_DESIGN.md:
explicit collaborator grants on a single recording session, on top of
the org-level and project-level membership checks that already exist.

Endpoints are mounted on /api/simple/recording-sessions/{session_id}
alongside the rest of the simple-recording API so the frontend can
hang the share modal off the same base path.

Also exposes `has_session_access(...)`, the single helper other
endpoints should call when they need to decide whether a non-org user
is allowed to see or mutate a recording session.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field, model_validator
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User, UserOrganization
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import RecordingSession as DBRecordingSession
from database.models import SessionCollaborator
from services.invitation_tokens import (
    INVITATION_TOKEN_VERSION,
    INVITATION_ISSUANCE_UNAVAILABLE,
    build_invitation_url,
    generate_invitation_secret,
    hash_invitation_secret,
    invitation_delivery_url_is_allowed,
    invitation_resend_minimum_interval_seconds,
    invitation_v2_issuance_enabled,
    legacy_invitation_is_allowed,
    smtp_plaintext_is_allowed,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/simple/recording-sessions/{session_id}",
    tags=["session-permissions"],
)

# Routes that resolve a public token (no auth) live on a separate
# router so they aren't shadowed by the path-prefixed router above.
public_router = APIRouter(
    prefix="/api/simple/recording-sessions",
    tags=["session-permissions"],
)


# ---------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------


AccessLevel = Literal["read", "comment", "edit"]
_VALID_ACCESS_LEVELS = ("read", "comment", "edit")
DeliveryState = Literal[
    "pending",
    "sent",
    "failed",
    "accepted",
    "revoked",
    "expired",
]


class CollaboratorCreate(BaseModel):
    """Body for POST /permissions/collaborators.

    Either user_id or email must be set. If both are set, user_id wins
    and the email is kept for display only.
    """
    user_id: Optional[int] = None
    email: Optional[EmailStr] = None
    access_level: AccessLevel = "read"

    @model_validator(mode="after")
    def _require_user_or_email(self) -> "CollaboratorCreate":
        if not self.user_id and not self.email:
            raise ValueError("Either user_id or email must be provided")
        return self


class CollaboratorPatch(BaseModel):
    """Body for PATCH /permissions/collaborators/{id}."""
    access_level: AccessLevel


class CollaboratorUserOut(BaseModel):
    id: int
    email: str
    username: Optional[str] = None
    full_name: Optional[str] = None


class CollaboratorOut(BaseModel):
    id: int
    user: Optional[CollaboratorUserOut] = None
    email: Optional[str] = None
    access_level: AccessLevel
    expires_at: Optional[str] = None
    accepted_at: Optional[str] = None
    revoked_at: Optional[str] = None
    created_at: Optional[str] = None
    accepted: bool = False
    delivery_state: DeliveryState
    delivery_attempt_count: int = 0
    last_delivery_attempt_at: Optional[str] = None
    delivery_failure_reason: Optional[str] = None


class CollaboratorCreateOut(CollaboratorOut):
    """Creation response.

    `invite_url_once` is populated only when this request created a new
    external invitation secret. It is absent from all list/update responses.
    """

    created: bool
    invite_url_once: Optional[str] = None
    delivered: Optional[bool] = None


class ResendOut(BaseModel):
    collaborator: CollaboratorOut
    invite_url_once: str
    delivered: bool


class PermissionsOut(BaseModel):
    session_id: str
    # True if any org admin/manager/member/viewer of the session's org
    # can see it. Always true today for org-scoped sessions, but exposed
    # so the frontend can render the "shared with your team" badge
    # accurately when we add per-session visibility flags later.
    org_default: bool
    # True if a project link is set and any member of that project can
    # see it. Always False today because the cross-app project member
    # lookup isn't wired yet (see TODO in has_session_access).
    project_default: bool
    collaborators: List[CollaboratorOut]


class AccessTokenOut(BaseModel):
    """Response for POST /permissions/access.

    Public, no auth. A client may use this to verify a magic link
    before redirecting the viewer into the shared-session view.
    """
    valid: bool
    session_id: Optional[str] = None
    session_db_id: Optional[int] = None
    access_level: Optional[AccessLevel] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _get_session_by_str_id(db: Session, session_id: str) -> Optional[DBRecordingSession]:
    """Resolve a session by its UUID string-id or its integer PK.

    Mirrors _get_session_for_org in simple_recording_db.py but does NOT
    scope by organization — callers do their own access check after.
    """
    session = (
        db.query(DBRecordingSession)
        .filter(DBRecordingSession.session_id == session_id)
        .first()
    )
    if session:
        return session
    try:
        int_id = int(session_id)
    except (TypeError, ValueError):
        return None
    return (
        db.query(DBRecordingSession)
        .filter(DBRecordingSession.id == int_id)
        .first()
    )


def _user_org_role(
    db: Session, user: User, organization_id: Optional[int]
) -> Optional[str]:
    """Return the user's role in `organization_id`, or None if no membership."""
    if organization_id is None:
        return None
    membership = (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id == user.id,
            UserOrganization.organization_id == organization_id,
        )
        .first()
    )
    return membership.role if membership else None


def _is_project_member(
    db: Session,
    user: User,
    project_app: Optional[str],
    project_id: Optional[str],
) -> bool:
    """Stub for the cross-app project membership lookup.

    TODO: call out to project-ops / crisis-ops (or look up a shared
    project_members table once one exists) to decide whether `user`
    is a member of the linked project. Until that's wired we treat
    "no project members visible" as the safe default — meeting will
    still be visible via org membership or an explicit collaborator
    row.
    """
    return False


def has_session_access(
    session_id: str | int,
    user: User,
    db: Session,
) -> Literal["denied", "view", "comment", "edit"]:
    """Decide what level of access `user` has to a recording session.

    Resolution order (first match wins):
      1. Org admin / manager / member of the session's org -> "edit"
      2. Org viewer of the session's org                  -> "view"
      3. Project member (via cross-app lookup, stubbed)   -> "edit"
      4. session_collaborators row for this user          -> row.access_level
      5. (else)                                           -> "denied"

    Superusers always get "edit".

    Callers should treat anything other than "denied" as "may at least
    read" and use the specific level to decide whether to allow mutations.
    """
    # Resolve session
    if isinstance(session_id, int):
        session = (
            db.query(DBRecordingSession)
            .filter(DBRecordingSession.id == session_id)
            .first()
        )
    else:
        session = _get_session_by_str_id(db, str(session_id))
    if not session:
        return "denied"

    # Superuser shortcut — global admin
    if getattr(user, "is_superuser", False):
        return "edit"

    # 1+2: org membership
    role = _user_org_role(db, user, session.organization_id)
    if role in ("admin", "manager", "member", "user"):
        # "user" is the legacy role name in user_organizations.role;
        # treat it equivalent to "member" for session access.
        return "edit"
    if role == "viewer":
        return "view"

    # 3: project membership (stubbed)
    if session.project_app and session.project_id:
        if _is_project_member(db, user, session.project_app, session.project_id):
            return "edit"

    # 4: explicit collaborator row
    collaborator = (
        db.query(SessionCollaborator)
        .filter(
            SessionCollaborator.session_id == session.id,
            SessionCollaborator.revoked_at.is_(None),
            SessionCollaborator.user_id == user.id,
        )
        .first()
    )
    if collaborator and _is_collaborator_active(collaborator):
        level = collaborator.access_level or "read"
        if level == "read":
            return "view"
        if level == "comment":
            return "comment"
        if level == "edit":
            return "edit"

    return "denied"


_ACCESS_RANK = {"denied": 0, "view": 1, "comment": 2, "edit": 3}


def resolve_session_for_user(
    db: Session,
    active_org_id: int,
    session_id: str | int,
    user: User,
    min_level: str = "view",
) -> Optional[DBRecordingSession]:
    """Canonical session resolver for session-scoped endpoints.

    1. Strict lookup inside the caller's ACTIVE org (uuid session_id column,
       then integer pk) — org members keep exactly the access they had.
    2. Cross-org fallback: resolve by id alone, then gate on
       has_session_access >= min_level ("view" for reads, "edit" for
       mutations). Covers personal-org sessions viewed while active in
       another org, and shared/collaborator sessions.

    Returns None when nothing accessible matches — callers raise their own
    404 so response shapes stay unchanged. This exists because the strict
    filter was copy-pasted into half a dozen files (audio download, tags,
    attachments, TTS, insights, chat, reprocess, rediarize…) and each copy
    404'd cross-org sessions the main session GET happily serves.
    """
    q = db.query(DBRecordingSession).filter(
        DBRecordingSession.organization_id == active_org_id
    )
    session = q.filter(DBRecordingSession.session_id == str(session_id)).first()
    if not session:
        try:
            pk = int(session_id)
        except (TypeError, ValueError):
            pk = None
        if pk is not None:
            session = q.filter(DBRecordingSession.id == pk).first()
    if session:
        return session

    candidate = _get_session_by_str_id(db, str(session_id))
    if not candidate:
        return None
    level = has_session_access(candidate.id, user, db)
    if _ACCESS_RANK.get(level, 0) >= _ACCESS_RANK.get(min_level, 1):
        return candidate
    return None


def _is_collaborator_active(row: SessionCollaborator) -> bool:
    if row.revoked_at is not None:
        return False
    if row.expires_at is not None:
        now = datetime.now(timezone.utc)
        # row.expires_at may come back naive on some drivers — normalize.
        exp = row.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < now:
            return False
    return True


def _normalize_invitation_email(value: Optional[str]) -> Optional[str]:
    normalized = (value or "").strip().casefold()
    return normalized or None


def _verified_user_for_email(
    db: Session,
    email: Optional[str],
) -> Optional[User]:
    """Resolve an email identity only to an active, verified account."""
    normalized = _normalize_invitation_email(email)
    if not normalized:
        return None
    return (
        db.query(User)
        .filter(
            func.lower(User.email) == normalized,
            User.is_active.is_(True),
            User.is_verified.is_(True),
        )
        .order_by(User.id.asc())
        .first()
    )


def _lock_invitation_parent(
    db: Session,
    session_id: int,
) -> DBRecordingSession:
    """Serialize invitation identity changes on the parent meeting row."""
    return (
        db.query(DBRecordingSession)
        .filter(DBRecordingSession.id == session_id)
        .with_for_update()
        .populate_existing()
        .one()
    )


def _identity_emails(
    db: Session,
    *,
    user_id: Optional[int],
    email: Optional[str],
) -> set[str]:
    emails: set[str] = set()
    normalized = _normalize_invitation_email(email)
    if normalized:
        emails.add(normalized)
    if user_id is not None:
        user = db.query(User).filter(User.id == user_id).first()
        user_email = _normalize_invitation_email(user.email if user else None)
        if user_email:
            emails.add(user_email)
    return emails


def _identity_rows(
    db: Session,
    *,
    session_id: int,
    user_id: Optional[int],
    emails: set[str],
    include_revoked: bool = False,
) -> list[SessionCollaborator]:
    conditions = []
    if user_id is not None:
        conditions.append(SessionCollaborator.user_id == user_id)
    conditions.extend(
        func.lower(SessionCollaborator.email) == email
        for email in sorted(emails)
    )
    if not conditions:
        return []
    query = db.query(SessionCollaborator).filter(
        SessionCollaborator.session_id == session_id,
        or_(*conditions),
    )
    if not include_revoked:
        query = query.filter(SessionCollaborator.revoked_at.is_(None))
    return query.order_by(SessionCollaborator.id.asc()).with_for_update().all()


def _revoke_invitation_row(
    row: SessionCollaborator,
    *,
    now: datetime,
) -> None:
    if row.revoked_at is None:
        row.revoked_at = now
    row.delivery_state = "revoked"
    row.delivery_failure_reason = None
    # Preserve the digest for a controlled terminal "revoked" result, while
    # rotating the legacy compatibility UUID away from any prior plaintext.
    row.token = uuid.uuid4()


def _consolidate_invitation_identity(
    db: Session,
    *,
    session_id: int,
    user_id: Optional[int],
    email: Optional[str],
) -> Optional[SessionCollaborator]:
    """Return one deterministic active grant and revoke every sibling.

    Callers must first lock the parent recording_sessions row. That gives both
    issuance surfaces the same race boundary without relying on an unsafe
    partial unique index over legacy data.
    """
    emails = _identity_emails(db, user_id=user_id, email=email)
    rows = _identity_rows(
        db,
        session_id=session_id,
        user_id=user_id,
        emails=emails,
    )
    active_rows = [row for row in rows if _is_collaborator_active(row)]
    if active_rows:
        # Prefer an already user-bound/accepted grant, then the oldest row.
        # This keeps accepted access stable and makes consolidation independent
        # of query planner or request ordering.
        survivor = min(
            active_rows,
            key=lambda row: (
                0
                if row.user_id is not None or row.accepted_at is not None
                else 1,
                row.id,
            ),
        )
    else:
        survivor = None

    now = datetime.now(timezone.utc)
    for row in rows:
        if survivor is None or row.id != survivor.id:
            _revoke_invitation_row(row, now=now)
    return survivor


def _require_v2_invitation_issuance() -> None:
    if not invitation_v2_issuance_enabled():
        raise HTTPException(
            status_code=503,
            detail=INVITATION_ISSUANCE_UNAVAILABLE,
        )


def _can_manage_collaborators(
    db: Session,
    user: User,
    session: DBRecordingSession,
) -> bool:
    """Restricted to org admin/manager OR the session creator."""
    if getattr(user, "is_superuser", False):
        return True
    if session.user_id and session.user_id == user.id:
        return True
    role = _user_org_role(db, user, session.organization_id)
    return role in ("admin", "manager")


def _serialize_collaborator(
    db: Session, row: SessionCollaborator
) -> CollaboratorOut:
    user_out: Optional[CollaboratorUserOut] = None
    if row.user_id:
        u = db.query(User).filter(User.id == row.user_id).first()
        if u:
            user_out = CollaboratorUserOut(
                id=u.id,
                email=u.email,
                username=u.username,
                full_name=u.full_name,
            )
    return CollaboratorOut(
        id=row.id,
        user=user_out,
        email=row.email,
        access_level=row.access_level,  # type: ignore[arg-type]
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        accepted_at=row.accepted_at.isoformat() if row.accepted_at else None,
        revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
        created_at=row.created_at.isoformat() if row.created_at else None,
        accepted=row.accepted_at is not None,
        delivery_state=_effective_delivery_state(row),
        delivery_attempt_count=row.delivery_attempt_count or 0,
        last_delivery_attempt_at=(
            row.last_delivery_attempt_at.isoformat()
            if row.last_delivery_attempt_at
            else None
        ),
        delivery_failure_reason=row.delivery_failure_reason,
    )


def _effective_delivery_state(row: SessionCollaborator) -> DeliveryState:
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is not None:
        expires_at = row.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return "expired"
    # Legacy direct user grants did not always populate accepted_at. A bound
    # user_id is already an accepted grant and must never become resendable.
    if row.user_id is not None or row.accepted_at is not None:
        return "accepted"
    state = row.delivery_state or "pending"
    if state not in {"pending", "sent", "failed"}:
        return "pending"
    return state  # type: ignore[return-value]


def _find_invitation_by_secret(
    db: Session,
    secret: str,
    *,
    lock: bool = False,
) -> Optional[SessionCollaborator]:
    """Hash a presented secret and resolve it without plaintext lookup."""
    digest = hash_invitation_secret(secret)
    query = db.query(SessionCollaborator).filter(
        SessionCollaborator.token_hash == digest
    )
    if lock:
        query = query.with_for_update().populate_existing()
    row = query.first()
    if row and legacy_invitation_is_allowed(row.token_version):
        return row
    return None


def _resend_minimum_interval_seconds() -> int:
    """Compatibility wrapper for the explicit collaborator resend path."""
    return invitation_resend_minimum_interval_seconds()


# ---------------------------------------------------------------------
# Invitation delivery
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class InvitationDeliveryResult:
    sent: bool
    failure_reason: Optional[str] = None


def _send_invitation_email(
    *,
    to_email: str,
    invite_url: str,
    inviter: User,
    session: DBRecordingSession,
    access_level: str,
) -> InvitationDeliveryResult:
    """Send an external-collaborator invitation email.

    Exactly one configured transport is attempted. A failed or ambiguous
    Postmark attempt never falls through to SMTP because doing so could
    duplicate a delivery that the first provider actually accepted.

    Only bounded operator-safe reason codes are returned for persistence.
    """
    from auth.email import _postmark_sender, _postmark_token

    if not invitation_delivery_url_is_allowed(invite_url):
        logger.warning(
            "invitation delivery skipped reason=public_url_not_configured "
            "session=%s",
            session.session_id or session.id,
        )
        return InvitationDeliveryResult(
            sent=False,
            failure_reason="public_url_not_configured",
        )

    postmark_token = _postmark_token()
    from_email = (
        _postmark_sender()
        if postmark_token
        else (
            os.getenv("SMTP_FROM", "").strip()
            or os.getenv("SMTP_USER", "").strip()
        )
    )
    subject = (
        f"{inviter.full_name or inviter.email} shared a meeting with you"
    )
    session_title = (
        session.title or session.name or f"Meeting {session.id}"
    )
    body_text = (
        f"{inviter.full_name or inviter.email} invited you to view a "
        f"meeting on Meeting-Ops ({access_level} access).\n\n"
        f"Meeting: {session_title}\n\n"
        f"Open it here: {invite_url}\n\n"
        "If you weren't expecting this, you can ignore this email."
    )

    if not from_email:
        logger.warning(
            "invitation delivery skipped reason=sender_not_configured "
            "session=%s",
            session.session_id or session.id,
        )
        return InvitationDeliveryResult(
            sent=False,
            failure_reason="delivery_not_configured",
        )

    if postmark_token:
        try:
            import json as _json
            import urllib.request as _u

            req = _u.Request(
                "https://api.postmarkapp.com/email",
                data=_json.dumps(
                    {
                        "From": from_email,
                        "To": to_email,
                        "Subject": subject,
                        "TextBody": body_text,
                        "MessageStream": os.getenv(
                            "POSTMARK_MESSAGE_STREAM", "outbound"
                        ),
                    }
                ).encode(),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Postmark-Server-Token": postmark_token,
                },
                method="POST",
            )
            with _u.urlopen(req, timeout=10) as resp:
                if resp.status >= 300:
                    raise RuntimeError(f"Postmark HTTP {resp.status}")
            logger.info(
                "invitation delivery sent session=%s",
                session.session_id or session.id,
            )
            return InvitationDeliveryResult(sent=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "invitation delivery failed session=%s error_type=%s",
                session.session_id or session.id,
                type(exc).__name__,
            )
            return InvitationDeliveryResult(
                sent=False,
                failure_reason="delivery_failed",
            )

    smtp_host = os.getenv("SMTP_HOST", "").strip()
    if not smtp_host:
        logger.warning(
            "invitation delivery skipped reason=delivery_not_configured "
            "session=%s",
            session.session_id or session.id,
        )
        return InvitationDeliveryResult(
            sent=False,
            failure_reason="delivery_not_configured",
        )

    try:
        import smtplib
        from email.mime.text import MIMEText

        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        smtp_user = os.getenv("SMTP_USER", "").strip()
        smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
        use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
        if not use_tls and not smtp_plaintext_is_allowed(smtp_host):
            logger.warning(
                "invitation delivery skipped reason=insecure_smtp_not_allowed "
                "session=%s",
                session.session_id or session.id,
            )
            return InvitationDeliveryResult(
                sent=False,
                failure_reason="insecure_smtp_not_allowed",
            )

        msg = MIMEText(body_text)
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = to_email

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as smtp:
            if use_tls:
                import ssl

                smtp.starttls(context=ssl.create_default_context())
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)
        logger.info(
            "invitation delivery sent session=%s",
            session.session_id or session.id,
        )
        return InvitationDeliveryResult(sent=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "invitation delivery failed session=%s error_type=%s",
            session.session_id or session.id,
            type(exc).__name__,
        )
        return InvitationDeliveryResult(
            sent=False,
            failure_reason="delivery_failed",
        )


def _apply_delivery_result(
    row: SessionCollaborator,
    result: InvitationDeliveryResult,
) -> None:
    row.delivery_state = "sent" if result.sent else "failed"
    safe_reasons = {
        "delivery_not_configured",
        "delivery_failed",
        "delivery_internal_error",
        "public_url_not_configured",
        "insecure_smtp_not_allowed",
    }
    row.delivery_failure_reason = (
        None
        if result.sent
        else (
            result.failure_reason
            if result.failure_reason in safe_reasons
            else "delivery_failed"
        )
    )


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------


@router.get("/permissions", response_model=PermissionsOut)
async def get_permissions(
    session_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> PermissionsOut:
    """Return the collaborator-management view for a recording session.

    This payload includes the collaborator roster, so it is restricted to the
    session creator or an org admin/manager. It never includes invitation
    plaintext, hashes, provider responses, or internal provider metadata.
    """
    session = _get_session_by_str_id(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not _can_manage_collaborators(db, current_user, session):
        raise HTTPException(
            status_code=403,
            detail="Only the session creator or an org admin/manager can manage collaborators",
        )

    collaborators = (
        db.query(SessionCollaborator)
        .filter(SessionCollaborator.session_id == session.id)
        .order_by(SessionCollaborator.created_at.asc())
        .all()
    )

    project_default = False  # see _is_project_member TODO

    return PermissionsOut(
        session_id=session.session_id or str(session.id),
        org_default=session.organization_id is not None,
        project_default=project_default,
        collaborators=[_serialize_collaborator(db, c) for c in collaborators],
    )


@router.post(
    "/permissions/collaborators",
    response_model=CollaboratorCreateOut,
)
async def add_collaborator(
    session_id: str,
    body: CollaboratorCreate,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> CollaboratorCreateOut:
    """Add a collaborator to a recording session.

    - If body.user_id is set, the row is created with that user.
    - If only body.email is set and matches an existing user, user_id is
      auto-resolved (the invitee already has a uchub account).
    - If only body.email is set and no user matches, the row stays email-only
      and a copy-once magic-link secret gates access until acceptance.

    Restricted to org admin/manager OR the session creator.
    """
    session = _get_session_by_str_id(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not _can_manage_collaborators(db, current_user, session):
        raise HTTPException(
            status_code=403,
            detail="Only the session creator or an org admin/manager can manage collaborators",
        )

    if body.access_level not in _VALID_ACCESS_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"access_level must be one of {_VALID_ACCESS_LEVELS}",
        )

    # Every identity lookup and mutation is serialized on the parent session
    # row. This is the shared race boundary used by both invite issuance paths.
    session = _lock_invitation_parent(db, session.id)

    user_id = body.user_id
    email_value = _normalize_invitation_email(
        str(body.email) if body.email else None
    )
    resolved_email = email_value

    if user_id:
        invitee = db.query(User).filter(User.id == user_id).first()
        if not invitee:
            raise HTTPException(status_code=404, detail="Invitee user not found")
        resolved_email = _normalize_invitation_email(invitee.email)
    elif email_value:
        # An unverified account does not own an email invitation identity.
        # It must verify the matching address and present the bearer secret.
        invitee = _verified_user_for_email(db, email_value)
        if invitee:
            user_id = invitee.id
            resolved_email = _normalize_invitation_email(invitee.email)

    existing = _consolidate_invitation_identity(
        db,
        session_id=session.id,
        user_id=user_id,
        email=resolved_email,
    )

    # POST is idempotent for an already-active grant. Access may be updated,
    # but a reusable secret is never re-exposed and no delivery is retried.
    if existing:
        existing.access_level = body.access_level
        if user_id is not None:
            existing.user_id = user_id
            existing.email = resolved_email
            existing.accepted_at = existing.accepted_at or datetime.now(
                timezone.utc
            )
            existing.delivery_state = "accepted"
            existing.delivery_failure_reason = None
            # A direct user grant needs no bearer. Invalidate any older
            # email-only secret if this row was consolidated into it.
            existing.token_hash = None
            existing.token = uuid.uuid4()
        elif resolved_email:
            existing.email = resolved_email
        db.commit()
        db.refresh(existing)
        serialized = _serialize_collaborator(db, existing)
        return CollaboratorCreateOut(
            **serialized.model_dump(),
            created=False,
        )

    if user_id is None:
        _require_v2_invitation_issuance()

    now = datetime.now(timezone.utc)
    secret = generate_invitation_secret() if not user_id else None
    row = SessionCollaborator(
        session_id=session.id,
        user_id=user_id,
        email=str(resolved_email) if resolved_email else None,
        access_level=body.access_level,
        invited_by_user_id=current_user.id,
        # Compatibility UUID for the pre-scrub NOT NULL legacy column. It is
        # unrelated to the bearer secret and is never queried for validation.
        token=uuid.uuid4(),
        token_hash=hash_invitation_secret(secret) if secret else None,
        token_version=INVITATION_TOKEN_VERSION,
        accepted_at=now if user_id else None,
        delivery_state="accepted" if user_id else "pending",
        delivery_attempt_count=0 if user_id else 1,
        last_delivery_attempt_at=None if user_id else now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    delivery_result: Optional[InvitationDeliveryResult] = None
    if not user_id and resolved_email:
        try:
            delivery_url = build_invitation_url(
                secret or "",
                require_public=True,
            )
        except ValueError:
            delivery_result = InvitationDeliveryResult(
                sent=False,
                failure_reason="public_url_not_configured",
            )
        else:
            try:
                delivery_result = _send_invitation_email(
                    to_email=str(resolved_email),
                    invite_url=delivery_url,
                    inviter=current_user,
                    session=session,
                    access_level=body.access_level,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "invitation delivery failed unexpectedly session=%s "
                    "error_type=%s",
                    session.session_id or session.id,
                    type(exc).__name__,
                )
                delivery_result = InvitationDeliveryResult(
                    sent=False,
                    failure_reason="delivery_internal_error",
                )
        _apply_delivery_result(row, delivery_result)
        db.commit()
        db.refresh(row)

    serialized = _serialize_collaborator(db, row)
    return CollaboratorCreateOut(
        **serialized.model_dump(),
        created=True,
        invite_url_once=build_invitation_url(secret) if secret else None,
        delivered=delivery_result.sent if delivery_result else None,
    )


@router.patch(
    "/permissions/collaborators/{collaborator_id}",
    response_model=CollaboratorOut,
)
async def update_collaborator(
    session_id: str,
    collaborator_id: int,
    body: CollaboratorPatch,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> CollaboratorOut:
    """Change a collaborator's access_level. Same auth as POST."""
    session = _get_session_by_str_id(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not _can_manage_collaborators(db, current_user, session):
        raise HTTPException(
            status_code=403,
            detail="Only the session creator or an org admin/manager can manage collaborators",
        )
    if body.access_level not in _VALID_ACCESS_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"access_level must be one of {_VALID_ACCESS_LEVELS}",
        )

    row = (
        db.query(SessionCollaborator)
        .filter(
            SessionCollaborator.id == collaborator_id,
            SessionCollaborator.session_id == session.id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Collaborator not found")
    if row.revoked_at is not None:
        raise HTTPException(status_code=409, detail="Collaborator is revoked")

    row.access_level = body.access_level
    db.commit()
    db.refresh(row)
    return _serialize_collaborator(db, row)


@router.post(
    "/permissions/collaborators/{collaborator_id}/resend",
    response_model=ResendOut,
)
async def resend_collaborator_invitation(
    session_id: str,
    collaborator_id: int,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> ResendOut:
    """Rotate and deliver a fresh external invitation.

    The row is locked while the rate-limit/idempotency decision is recorded.
    The provider call happens only after the new attempt timestamp commits, so
    concurrent or repeated requests cannot create a second grant or send again.
    """
    session = _get_session_by_str_id(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not _can_manage_collaborators(db, current_user, session):
        raise HTTPException(
            status_code=403,
            detail="Only the session creator or an org admin/manager can manage collaborators",
        )

    session = _lock_invitation_parent(db, session.id)
    _require_v2_invitation_issuance()

    row = (
        db.query(SessionCollaborator)
        .filter(
            SessionCollaborator.id == collaborator_id,
            SessionCollaborator.session_id == session.id,
        )
        .with_for_update()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Collaborator not found")

    canonical = _consolidate_invitation_identity(
        db,
        session_id=session.id,
        user_id=row.user_id,
        email=row.email,
    )
    if canonical is not None and canonical.id != row.id:
        # Persist deterministic cleanup, then require the caller to refresh the
        # roster rather than rotating a now-revoked duplicate.
        db.commit()
        raise HTTPException(
            status_code=409,
            detail="Duplicate invitation was consolidated; refresh and retry",
        )

    state = _effective_delivery_state(row)
    if state in {"accepted", "revoked", "expired"}:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot resend an invitation in {state} state",
        )
    if not row.email:
        raise HTTPException(
            status_code=409,
            detail="Collaborator has no invitation email",
        )

    now = datetime.now(timezone.utc)
    minimum_interval = _resend_minimum_interval_seconds()
    if row.last_delivery_attempt_at is not None:
        last_attempt = row.last_delivery_attempt_at
        if last_attempt.tzinfo is None:
            last_attempt = last_attempt.replace(tzinfo=timezone.utc)
        retry_at = last_attempt + timedelta(seconds=minimum_interval)
        if retry_at > now:
            retry_after = max(1, int((retry_at - now).total_seconds()) + 1)
            raise HTTPException(
                status_code=429,
                detail="Invitation was attempted recently",
                headers={"Retry-After": str(retry_after)},
            )

    secret = generate_invitation_secret()
    row.token = uuid.uuid4()
    row.token_hash = hash_invitation_secret(secret)
    row.token_version = INVITATION_TOKEN_VERSION
    row.delivery_state = "pending"
    row.delivery_failure_reason = None
    row.last_delivery_attempt_at = now
    row.delivery_attempt_count = (row.delivery_attempt_count or 0) + 1
    db.commit()

    try:
        delivery_url = build_invitation_url(secret, require_public=True)
    except ValueError:
        result = InvitationDeliveryResult(
            sent=False,
            failure_reason="public_url_not_configured",
        )
    else:
        try:
            result = _send_invitation_email(
                to_email=str(row.email),
                invite_url=delivery_url,
                inviter=current_user,
                session=session,
                access_level=row.access_level,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "invitation resend failed unexpectedly session=%s error_type=%s",
                session.session_id or session.id,
                type(exc).__name__,
            )
            result = InvitationDeliveryResult(
                sent=False,
                failure_reason="delivery_internal_error",
            )

    row = (
        db.query(SessionCollaborator)
        .filter(
            SessionCollaborator.id == collaborator_id,
            SessionCollaborator.session_id == session.id,
        )
        .first()
    )
    if not row or row.revoked_at is not None:
        # A concurrent revocation wins. Do not resurrect the invitation state.
        raise HTTPException(status_code=409, detail="Invitation was revoked")
    _apply_delivery_result(row, result)
    db.commit()
    db.refresh(row)

    return ResendOut(
        collaborator=_serialize_collaborator(db, row),
        invite_url_once=build_invitation_url(secret),
        delivered=result.sent,
    )


@router.delete("/permissions/collaborators/{collaborator_id}")
async def revoke_collaborator(
    session_id: str,
    collaborator_id: int,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> dict:
    """Soft-revoke a collaborator (sets revoked_at). Same auth as POST."""
    session = _get_session_by_str_id(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if not _can_manage_collaborators(db, current_user, session):
        raise HTTPException(
            status_code=403,
            detail="Only the session creator or an org admin/manager can manage collaborators",
        )

    session = _lock_invitation_parent(db, session.id)
    row = (
        db.query(SessionCollaborator)
        .filter(
            SessionCollaborator.id == collaborator_id,
            SessionCollaborator.session_id == session.id,
        )
        .with_for_update()
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Collaborator not found")

    emails = _identity_emails(db, user_id=row.user_id, email=row.email)
    identity_rows = _identity_rows(
        db,
        session_id=session.id,
        user_id=row.user_id,
        emails=emails,
    )
    now = datetime.now(timezone.utc)
    for identity_row in identity_rows:
        _revoke_invitation_row(identity_row, now=now)
    db.commit()
    return {"status": "revoked", "collaborator_id": collaborator_id}


class RedeemTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=256)


class RedeemTokenResponse(BaseModel):
    valid: bool
    session_id: Optional[str] = None
    session_db_id: Optional[int] = None
    access_level: Optional[AccessLevel] = None
    reason: Optional[str] = None
    bound_user_id: Optional[int] = None


@public_router.post(
    "/permissions/redeem",
    response_model=RedeemTokenResponse,
)
async def redeem_magic_link(
    payload: RedeemTokenRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Auth'd redemption of a magic-link share token. The frontend calls
    this after Keycloak login on the shared-session landing page.

    Binds the current user to the collaborator row (so subsequent
    requests authorize through user_id rather than relying on the
    token-in-URL pattern) and returns the session id + granted access
    level. Idempotent.
    """
    # The row lock makes the user binding a deterministic first-writer
    # operation. A retry by that same user is idempotent; a different account
    # cannot overwrite the established binding.
    row = _find_invitation_by_secret(db, payload.token, lock=True)
    if not row:
        return RedeemTokenResponse(valid=False, reason="invitation not found")
    if not _is_collaborator_active(row):
        reason = "revoked" if row.revoked_at is not None else "expired"
        return RedeemTokenResponse(valid=False, reason=reason)

    session = (
        db.query(DBRecordingSession)
        .filter(DBRecordingSession.id == row.session_id)
        .first()
    )
    if not session:
        return RedeemTokenResponse(valid=False, reason="session not found")

    if row.user_id is not None:
        if row.user_id != current_user.id:
            return RedeemTokenResponse(
                valid=False,
                reason="this invitation belongs to a different account",
            )
        # Already bound to this account: continue to the same successful
        # response without changing ownership.
    elif row.email:
        current_email = _normalize_invitation_email(current_user.email)
        if (
            not getattr(current_user, "is_verified", False)
            or not current_email
            or _normalize_invitation_email(row.email) != current_email
        ):
            return RedeemTokenResponse(
                valid=False,
                reason="this invitation requires a verified matching email",
            )

    # Bind the user, mark accepted.
    if row.user_id != current_user.id:
        row.user_id = current_user.id
    if not row.accepted_at:
        row.accepted_at = datetime.now(timezone.utc)
    row.delivery_state = "accepted"
    row.delivery_failure_reason = None
    db.commit()

    return RedeemTokenResponse(
        valid=True,
        session_id=session.session_id or str(session.id),
        session_db_id=session.id,
        access_level=row.access_level,
        bound_user_id=current_user.id,
    )


@public_router.post(
    "/permissions/access",
    response_model=AccessTokenOut,
)
async def resolve_access_token(
    payload: RedeemTokenRequest,
    db: Session = Depends(get_db),
) -> AccessTokenOut:
    """Resolve a magic-link share secret. Public — no auth required.

    The secret is accepted only in a JSON request body so reverse-proxy request
    lines do not capture it.
    """
    row = _find_invitation_by_secret(db, payload.token)
    if not row:
        return AccessTokenOut(valid=False, reason="invitation not found")
    if not _is_collaborator_active(row):
        reason = (
            "revoked"
            if row.revoked_at is not None
            else "expired"
        )
        return AccessTokenOut(valid=False, reason=reason)

    session = (
        db.query(DBRecordingSession)
        .filter(DBRecordingSession.id == row.session_id)
        .first()
    )
    if not session:
        return AccessTokenOut(valid=False, reason="session not found")

    return AccessTokenOut(
        valid=True,
        session_id=session.session_id or str(session.id),
        session_db_id=session.id,
        access_level=row.access_level,  # type: ignore[arg-type]
    )
