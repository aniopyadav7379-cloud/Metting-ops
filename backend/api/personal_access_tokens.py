"""Personal Access Token management endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import get_current_active_user
from auth.models import Organization, PersonalAccessToken, User, UserOrganization
from auth.pat import create_pat, revoke_pat, rotate_pat_atomically
from database.database import get_db

router = APIRouter(prefix="/api/auth/pats", tags=["personal-access-tokens"])


class CreatePATRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scope: Literal["user", "stable.transcript.ingest", "omi.transcript.ingest"] = "user"
    organization_slug: Optional[str] = Field(default=None, max_length=128)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=365)


class PATCreateResponse(BaseModel):
    id: int
    name: str
    token_prefix: str
    plaintext: str
    created_at: datetime
    scope: str
    organization_id: Optional[int] = None
    expires_at: Optional[datetime] = None
    rotated_from_id: Optional[int] = None


class PATListItem(BaseModel):
    id: int
    name: str
    token_prefix: str
    last_used_at: Optional[datetime] = None
    created_at: datetime
    revoked_at: Optional[datetime] = None
    scope: str
    organization_id: Optional[int] = None
    expires_at: Optional[datetime] = None
    rotated_from_id: Optional[int] = None


def _to_item(row: PersonalAccessToken) -> PATListItem:
    return PATListItem(
        id=row.id,
        name=row.name,
        token_prefix=row.token_prefix,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        scope=row.scope,
        organization_id=row.organization_id,
        expires_at=row.expires_at,
        rotated_from_id=row.rotated_from_id,
    )


def _integration_binding(
    db: Session,
    *,
    user: User,
    body: CreatePATRequest,
) -> tuple[int | None, datetime | None]:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    if body.scope == "user":
        if body.organization_slug:
            raise HTTPException(
                status_code=400,
                detail="organization_slug is only valid for integration PATs",
            )
        return None, expires_at

    if not body.organization_slug:
        raise HTTPException(
            status_code=400,
            detail="organization_slug is required for integration PATs",
        )
    if expires_at is None:
        raise HTTPException(
            status_code=400,
            detail="expires_in_days is required for integration PATs",
        )
    organization = (
        db.query(Organization)
        .filter(
            Organization.slug == body.organization_slug.strip().lower(),
            Organization.is_active.is_(True),
        )
        .first()
    )
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    membership = (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id == user.id,
            UserOrganization.organization_id == organization.id,
        )
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="Not authorized for organization")
    role = (membership.role or "").strip().lower()
    if role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Organization admin role is required for integration PATs",
        )
    return organization.id, expires_at


@router.post("", response_model=PATCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_personal_access_token(
    body: CreatePATRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    organization_id, expires_at = _integration_binding(
        db,
        user=current_user,
        body=body,
    )
    row, plaintext = create_pat(
        db,
        user=current_user,
        name=body.name,
        scope=body.scope,
        organization_id=organization_id,
        expires_at=expires_at,
    )
    return PATCreateResponse(
        id=row.id,
        name=row.name,
        token_prefix=row.token_prefix,
        plaintext=plaintext,
        created_at=row.created_at,
        scope=row.scope,
        organization_id=row.organization_id,
        expires_at=row.expires_at,
        rotated_from_id=row.rotated_from_id,
    )


@router.post(
    "/{pat_id}/rotate",
    response_model=PATCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def rotate_personal_access_token(
    pat_id: int,
    expires_in_days: int = 90,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    old = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.id == pat_id,
            PersonalAccessToken.user_id == current_user.id,
            PersonalAccessToken.revoked_at.is_(None),
        )
        .first()
    )
    if old is None:
        raise HTTPException(status_code=404, detail="Token not found")
    if old.scope != "user":
        membership = (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == current_user.id,
                UserOrganization.organization_id == old.organization_id,
            )
            .first()
        )
        role = (
            (membership.role or "").strip().lower()
            if membership is not None
            else ""
        )
        if role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Organization admin role is required to rotate integration PATs",
            )
    if not 1 <= expires_in_days <= 365:
        raise HTTPException(status_code=422, detail="expires_in_days must be 1..365")
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=expires_in_days)
        if old.scope != "user" or old.expires_at is not None
        else None
    )
    row, plaintext = rotate_pat_atomically(
        db,
        old=old,
        user=current_user,
        expires_at=expires_at,
    )
    return PATCreateResponse(
        id=row.id,
        name=row.name,
        token_prefix=row.token_prefix,
        plaintext=plaintext,
        created_at=row.created_at,
        scope=row.scope,
        organization_id=row.organization_id,
        expires_at=row.expires_at,
        rotated_from_id=row.rotated_from_id,
    )


@router.get("", response_model=list[PATListItem])
async def list_personal_access_tokens(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    rows = (
        db.query(PersonalAccessToken)
        .filter(PersonalAccessToken.user_id == current_user.id)
        .order_by(PersonalAccessToken.created_at.desc(), PersonalAccessToken.id.desc())
        .all()
    )
    return [_to_item(row) for row in rows]


@router.delete("/{pat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_personal_access_token(
    pat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    revoke_pat(db, user=current_user, pat_id=pat_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
