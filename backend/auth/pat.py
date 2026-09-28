"""Personal Access Token helpers.

PAT plaintext is shown once at creation time. Only a SHA-256 digest is stored.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from auth.models import PersonalAccessToken, User


TOKEN_PREFIX = "mops_pat_"
TOKEN_RANDOM_CHARS = 32
TOKEN_PREFIX_DISPLAY_CHARS = 12
_BASE32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_pat(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _generate_plaintext() -> str:
    random_part = "".join(secrets.choice(_BASE32_ALPHABET) for _ in range(TOKEN_RANDOM_CHARS))
    return f"{TOKEN_PREFIX}{random_part}"


def _clean_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Token name must not be empty")
    if len(cleaned) > 100:
        raise HTTPException(status_code=400, detail="Token name must be <= 100 characters")
    return cleaned


def _build_pat(
    *,
    user: User,
    name: str,
    scope: str = "user",
    organization_id: int | None = None,
    expires_at: datetime | None = None,
    rotated_from_id: int | None = None,
) -> tuple[PersonalAccessToken, str]:
    plaintext = _generate_plaintext()
    model = PersonalAccessToken(
        user_id=user.id,
        name=_clean_name(name),
        token_hash=hash_pat(plaintext),
        token_prefix=plaintext[:TOKEN_PREFIX_DISPLAY_CHARS],
        scope=scope,
        organization_id=organization_id,
        expires_at=expires_at,
        rotated_from_id=rotated_from_id,
        created_at=_now(),
    )
    return model, plaintext


def create_pat(
    db: Session,
    *,
    user: User,
    name: str,
    scope: str = "user",
    organization_id: int | None = None,
    expires_at: datetime | None = None,
    rotated_from_id: int | None = None,
) -> tuple[PersonalAccessToken, str]:
    model, plaintext = _build_pat(
        user=user,
        name=name,
        scope=scope,
        organization_id=organization_id,
        expires_at=expires_at,
        rotated_from_id=rotated_from_id,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model, plaintext


def rotate_pat_atomically(
    db: Session,
    *,
    old: PersonalAccessToken,
    user: User,
    expires_at: datetime | None,
) -> tuple[PersonalAccessToken, str]:
    """Mint the replacement and revoke the predecessor in one transaction."""
    model, plaintext = _build_pat(
        user=user,
        name=old.name,
        scope=old.scope,
        organization_id=old.organization_id,
        expires_at=expires_at,
        rotated_from_id=old.id,
    )
    db.add(model)
    old.revoked_at = _now()
    db.commit()
    db.refresh(model)
    return model, plaintext


def resolve_pat_record(
    db: Session,
    *,
    plaintext: str,
    update_last_used: bool = True,
) -> Optional[PersonalAccessToken]:
    if not plaintext or not plaintext.startswith(TOKEN_PREFIX):
        return None

    token_hash = hash_pat(plaintext)
    row = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.token_hash == token_hash,
            PersonalAccessToken.revoked_at.is_(None),
        )
        .first()
    )
    if not row:
        return None
    expires_at = row.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= _now():
            return None
    user = row.user
    if not user or not user.is_active:
        return None
    if update_last_used:
        row.last_used_at = _now()
        db.commit()
        db.refresh(row)
    return row


def resolve_pat(db: Session, *, plaintext: str) -> Optional[User]:
    row = resolve_pat_record(db, plaintext=plaintext)
    # Generic application and MCP authentication accepts only user-scoped
    # credentials. Integration PATs are capability tokens and must be resolved
    # explicitly by their owning endpoint (Stable ingest calls
    # ``resolve_pat_record`` and checks its exact scope + org binding).
    if row is None or row.scope != "user":
        return None
    return row.user


def revoke_pat(db: Session, *, user: User, pat_id: int) -> bool:
    row = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.id == pat_id,
            PersonalAccessToken.user_id == user.id,
            PersonalAccessToken.revoked_at.is_(None),
        )
        .first()
    )
    if not row:
        return False
    row.revoked_at = _now()
    db.commit()
    return True
