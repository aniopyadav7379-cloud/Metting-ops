"""Password reset endpoints (v3.21.0).

Exercises:
  POST /api/auth/password/forgot        always 202, idempotent on non-match
  GET  /api/auth/password/reset/validate token-state machine
  POST /api/auth/password/reset         flips password + marks token used
  Rate limit (3 / hour per email) via in-proc fallback
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _models():
    from auth.models import PasswordResetToken, User
    from database.database import SessionLocal
    return User, PasswordResetToken, SessionLocal


@pytest.fixture(autouse=True)
def _isolated_rate_limit(monkeypatch):
    """Reset the in-process rate-limit bucket between tests so one test
    doesn't poison the next. Also unset REDIS_URL so we exercise the
    in-proc path consistently."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    from auth import routes as auth_routes
    auth_routes._PASSWORD_RESET_RL_INPROC.clear()
    yield
    auth_routes._PASSWORD_RESET_RL_INPROC.clear()


def _make_user(email_suffix: str | None = None):
    User, _PRT, SessionLocal = _models()
    from auth.utils import get_password_hash

    db = SessionLocal()
    try:
        uname = "pwreset" + uuid.uuid4().hex[:8]
        email = f"{uname}@example.com" if email_suffix is None else email_suffix
        user = User(
            email=email,
            username=uname,
            hashed_password=get_password_hash("OldPass1!"),
            is_active=True,
            is_verified=True,
            tier="free",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def _latest_token_for(user_id: int):
    _User, PasswordResetToken, SessionLocal = _models()
    db = SessionLocal()
    try:
        return (
            db.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user_id)
            .order_by(PasswordResetToken.id.desc())
            .first()
        )
    finally:
        db.close()


def test_forgot_returns_202_for_unknown_email(client):
    r = client.post(
        "/api/auth/password/forgot",
        json={"email": "nobody-here-" + uuid.uuid4().hex[:6] + "@example.com"},
    )
    assert r.status_code == 202
    assert "reset link" in r.json()["message"].lower()


def test_forgot_creates_token_for_known_user(client):
    user = _make_user()
    r = client.post(
        "/api/auth/password/forgot",
        json={"email": user.email},
    )
    assert r.status_code == 202
    token_row = _latest_token_for(user.id)
    assert token_row is not None
    assert token_row.used_at is None
    # Token expires roughly an hour from now.
    expires = token_row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    assert expires > datetime.now(timezone.utc)


def test_validate_returns_400_for_unknown_token(client):
    r = client.get("/api/auth/password/reset/validate?token=does-not-exist")
    assert r.status_code == 400


def test_validate_returns_410_for_expired_token(client):
    user = _make_user()
    # Issue, then manually expire the token to force the 410 branch.
    client.post("/api/auth/password/forgot", json={"email": user.email})
    User, PasswordResetToken, SessionLocal = _models()
    db = SessionLocal()
    try:
        row = (
            db.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user.id)
            .order_by(PasswordResetToken.id.desc())
            .first()
        )
        # We can't recover the plaintext, so monkey-patch the lookup:
        # easier to test the state machine by hitting reset/validate with
        # a known-bad token. We force the row expired and re-issue with
        # a deterministic plaintext via the helper.
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.add(row)
        db.commit()
    finally:
        db.close()

    # Issue a second token + expire it the same way to test 410.
    import secrets
    from auth.routes import _hash_reset_token
    plaintext = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        expired = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_reset_token(plaintext),
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            created_at=datetime.now(timezone.utc),
        )
        db.add(expired)
        db.commit()
    finally:
        db.close()
    r = client.get(f"/api/auth/password/reset/validate?token={plaintext}")
    assert r.status_code == 410


def test_reset_happy_path(client):
    """Issue a token via the helper directly so we can keep the plaintext,
    then submit it and confirm the user's password actually changes."""
    user = _make_user()
    import secrets
    from auth.routes import _hash_reset_token
    from auth.utils import verify_password
    User, PasswordResetToken, SessionLocal = _models()

    plaintext = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        row = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_reset_token(plaintext),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
    finally:
        db.close()

    new_password = "BrandNewPass2!"
    r = client.post(
        "/api/auth/password/reset",
        json={"token": plaintext, "new_password": new_password},
    )
    assert r.status_code == 200, r.text

    # New password works.
    db = SessionLocal()
    try:
        refreshed = db.query(User).filter(User.id == user.id).first()
        assert verify_password(new_password, refreshed.hashed_password)
        # Token now flagged used so the same token can't double-reset.
        latest = (
            db.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user.id)
            .order_by(PasswordResetToken.id.desc())
            .first()
        )
        assert latest.used_at is not None
    finally:
        db.close()

    # Re-using the token now returns 410 (used).
    r = client.post(
        "/api/auth/password/reset",
        json={"token": plaintext, "new_password": "DifferentPass3!"},
    )
    assert r.status_code == 410


def test_reset_rejects_weak_password(client):
    user = _make_user()
    import secrets
    from auth.routes import _hash_reset_token
    User, PasswordResetToken, SessionLocal = _models()

    plaintext = secrets.token_urlsafe(32)
    db = SessionLocal()
    try:
        row = PasswordResetToken(
            user_id=user.id,
            token_hash=_hash_reset_token(plaintext),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
    finally:
        db.close()

    r = client.post(
        "/api/auth/password/reset",
        json={"token": plaintext, "new_password": "short"},
    )
    assert r.status_code == 400


def test_forgot_rate_limited_to_three_per_hour(client):
    user = _make_user()
    payload = {"email": user.email}
    # 3 requests succeed (each returns 202).
    for _ in range(3):
        r = client.post("/api/auth/password/forgot", json=payload)
        assert r.status_code == 202
    # 4th still returns 202 (we never leak rate-limit state to the caller)
    # but should NOT have created a 4th token.
    User, PasswordResetToken, SessionLocal = _models()
    db = SessionLocal()
    try:
        before = (
            db.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user.id)
            .count()
        )
    finally:
        db.close()

    r = client.post("/api/auth/password/forgot", json=payload)
    assert r.status_code == 202
    db = SessionLocal()
    try:
        after = (
            db.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == user.id)
            .count()
        )
    finally:
        db.close()
    assert after == before
