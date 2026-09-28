"""Consumer self-serve signup tests (Option B).

Covers the new pieces wired for free-tier consumer signup:
  1. Email-verification token helpers (type-isolated from password reset).
  2. AuthService.create_user(personal_org=True): private per-user org +
     free tier + unverified, vs the legacy shared-default-org path.
  3. The register glue (self-serve → personal org + verification email),
     exercised by calling the route function directly so we don't depend on
     the import-time ALLOW_REGISTRATION/Depends binding.
  4. /api/auth/verify-email + /api/auth/resend-verification endpoints.

Auth already had lockout (authenticate_user) and a free-tier default
(User.tier column), so those aren't re-tested here.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# 1. Email-verification token helpers
# ---------------------------------------------------------------------------


def test_email_verification_token_roundtrip():
    from auth.utils import generate_email_verification_token, verify_email_verification_token

    tok = generate_email_verification_token("Alice@Example.com")
    assert verify_email_verification_token(tok) == "Alice@Example.com"


def test_verification_token_rejects_reset_token():
    """A password-reset token must NOT validate as an email-verification
    token (type isolation), and vice versa."""
    from auth.utils import (
        generate_password_reset_token,
        verify_email_verification_token,
        generate_email_verification_token,
        verify_password_reset_token,
    )

    reset = generate_password_reset_token("a@b.com")
    verify = generate_email_verification_token("a@b.com")
    assert verify_email_verification_token(reset) is None
    assert verify_password_reset_token(verify) is None


def test_verification_token_rejects_garbage():
    from auth.utils import verify_email_verification_token

    assert verify_email_verification_token("not-a-jwt") is None


# ---------------------------------------------------------------------------
# 2. create_user(personal_org=...)
# ---------------------------------------------------------------------------


def _u() -> str:
    return "su" + uuid.uuid4().hex[:8]


def test_create_user_personal_org_is_private_free_unverified(app):
    from auth.service import AuthService
    from auth.models import Organization, UserOrganization
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        uname = _u()
        user = AuthService.create_user(
            db, email=f"{uname}@example.com", username=uname,
            password="Password123", full_name="Sue Self",
            personal_org=True,
        )
        # free tier (column default) + unverified self-serve user
        assert user.tier == "free"
        assert user.is_verified is False
        # a PRIVATE personal org named after the convention, user is admin
        mem = (
            db.query(UserOrganization)
            .filter(UserOrganization.user_id == user.id)
            .all()
        )
        assert len(mem) == 1
        org = db.query(Organization).filter(Organization.id == mem[0].organization_id).first()
        assert org.slug == f"{uname}-personal"
        assert mem[0].role == "admin"
    finally:
        db.close()


def test_create_user_without_personal_org_uses_shared_default(app):
    from auth.service import AuthService
    from auth.models import Organization, UserOrganization
    from auth.organization import ensure_default_organization
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        uname = _u()
        user = AuthService.create_user(
            db, email=f"{uname}@example.com", username=uname,
            password="Password123", personal_org=False,
        )
        default_org = ensure_default_organization(db)
        mem = (
            db.query(UserOrganization)
            .filter(UserOrganization.user_id == user.id)
            .first()
        )
        assert mem.organization_id == default_org.id
    finally:
        db.close()


def test_create_user_duplicate_email_rejected(app):
    from auth.service import AuthService
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        uname = _u()
        AuthService.create_user(
            db, email=f"{uname}@example.com", username=uname,
            password="Password123", personal_org=True,
        )
        with pytest.raises(ValueError):
            AuthService.create_user(
                db, email=f"{uname}@example.com", username=_u(),
                password="Password123", personal_org=True,
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. register glue (direct function call — avoids import-time Depends binding)
# ---------------------------------------------------------------------------


def test_register_self_serve_provisions_personal_org_and_sends_email(app, monkeypatch):
    from auth import routes as routes_mod
    from auth.config import auth_config
    from database.database import SessionLocal

    monkeypatch.setattr(auth_config, "ALLOW_REGISTRATION", True)
    sent = {}

    def _fake_send(to_email, url):
        sent["to"] = to_email
        sent["url"] = url
        return True

    monkeypatch.setattr(routes_mod, "send_verification_email", _fake_send)

    db = SessionLocal()
    try:
        uname = _u()
        payload = routes_mod.UserCreate(
            email=f"{uname}@example.com", username=uname, password="Password123",
        )
        resp = asyncio.run(routes_mod.register(user_data=payload, db=db, current_user=None))
        assert resp.tier == "free"
        assert resp.is_verified is False
        # verification email was sent to the new address, link hits verify-email
        assert sent["to"] == f"{uname}@example.com"
        assert "/api/auth/verify-email?token=" in sent["url"]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. /verify-email + /resend-verification endpoints
# ---------------------------------------------------------------------------


def _seed_unverified_user(username: str):
    from auth.service import AuthService
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        return AuthService.create_user(
            db, email=f"{username}@example.com", username=username,
            password="Password123", personal_org=True,
        ).id
    finally:
        db.close()


def test_verify_email_flips_is_verified(client):
    from auth.utils import generate_email_verification_token
    from auth.models import User
    from database.database import SessionLocal

    uname = _u()
    uid = _seed_unverified_user(uname)
    tok = generate_email_verification_token(f"{uname}@example.com")

    resp = client.get(f"/api/auth/verify-email?token={tok}", follow_redirects=False)
    assert resp.status_code == 303
    assert "verify=success" in resp.headers["location"]

    db = SessionLocal()
    try:
        assert db.query(User).filter(User.id == uid).first().is_verified is True
    finally:
        db.close()


def test_verify_email_invalid_token_redirects_invalid(client):
    resp = client.get("/api/auth/verify-email?token=garbage", follow_redirects=False)
    assert resp.status_code == 303
    assert "verify=invalid" in resp.headers["location"]


def test_resend_verification_is_generic_and_sends(client, monkeypatch):
    from auth import routes as routes_mod

    sent = {}
    monkeypatch.setattr(
        routes_mod, "send_verification_email",
        lambda to, url: sent.update(to=to) or True,
    )
    uname = _u()
    _seed_unverified_user(uname)

    resp = client.post("/api/auth/resend-verification", json={"email": f"{uname}@example.com"})
    assert resp.status_code == 200
    assert "verification link has been sent" in resp.json()["message"]
    assert sent.get("to") == f"{uname}@example.com"


def test_resend_verification_unknown_email_same_message_no_send(client, monkeypatch):
    from auth import routes as routes_mod

    sent = {}
    monkeypatch.setattr(
        routes_mod, "send_verification_email",
        lambda to, url: sent.update(to=to) or True,
    )
    resp = client.post(
        "/api/auth/resend-verification",
        json={"email": f"nobody-{uuid.uuid4().hex[:6]}@example.com"},
    )
    # Same generic 200 (no account enumeration) and nothing sent.
    assert resp.status_code == 200
    assert "verification link has been sent" in resp.json()["message"]
    assert "to" not in sent
