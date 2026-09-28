"""Human approval gate for the Customer-Ops meeting-summary projection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.federation_meetings import (
    approved_summary_payload,
    summary_approval_digest,
)
from api.session_permissions import has_session_access
from api.sessions_participants import _can_edit, _resolve_session
from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db


router = APIRouter(
    prefix="/api/simple/recording-sessions/{session_id}/federation-summary-approval",
    tags=["federation-summary-approval"],
)


class SummaryApprovalOut(BaseModel):
    status: str
    approved_at: Optional[datetime] = None
    can_manage: bool


def _can_manage(
    session,
    current_user: User,
    active_org: ActiveOrganization,
    db: Session,
) -> bool:
    """Mirror the resolver-plus-editor gates used by PUT and DELETE.

    A same-org session reaches mutations through the active-org resolver and
    needs only the existing editor predicate. A cross-org share must also hold
    canonical per-session ``edit`` access; an admin role in the *active*,
    unrelated organization must not make a read-only share manageable.
    """

    if getattr(session, "organization_id", None) != active_org.organization.id:
        if has_session_access(session.id, current_user, db) != "edit":
            return False
    return _can_edit(session, current_user, active_org)


def _out(
    session,
    current_user: User,
    active_org: ActiveOrganization,
    db: Session,
) -> SummaryApprovalOut:
    status, _payload = approved_summary_payload(session)
    return SummaryApprovalOut(
        status=status,
        approved_at=(
            session.federation_summary_approved_at
            if status == "approved"
            else None
        ),
        can_manage=_can_manage(session, current_user, active_org, db),
    )


@router.get("", response_model=SummaryApprovalOut)
async def get_summary_approval(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(
        db,
        active_org.organization.id,
        session_id,
        current_user,
    )
    return _out(session, current_user, active_org, db)


@router.put("", response_model=SummaryApprovalOut)
async def approve_summary(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(
        db,
        active_org.organization.id,
        session_id,
        current_user,
        min_level="edit",
    )
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(
            status_code=403,
            detail="Not permitted to approve this summary",
        )
    digest = summary_approval_digest(session)
    if digest is None:
        raise HTTPException(
            status_code=409,
            detail="No summary is available to approve",
        )
    session.federation_summary_approved_digest = digest
    session.federation_summary_approved_at = datetime.now(timezone.utc)
    session.federation_summary_approved_by_user_id = current_user.id
    db.commit()
    db.refresh(session)
    return _out(session, current_user, active_org, db)


@router.delete("", response_model=SummaryApprovalOut)
async def revoke_summary_approval(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(
        db,
        active_org.organization.id,
        session_id,
        current_user,
        min_level="edit",
    )
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(
            status_code=403,
            detail="Not permitted to revoke this summary approval",
        )
    session.federation_summary_approved_digest = None
    session.federation_summary_approved_at = None
    session.federation_summary_approved_by_user_id = None
    db.commit()
    db.refresh(session)
    return _out(session, current_user, active_org, db)
