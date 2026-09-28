"""Tests for the per-org integrations API (v3.19.0).

Covers:
  - GET masks secrets (last 4 chars only, has_api_key boolean)
  - PUT requires admin role (403 for plain users)
  - PUT encrypts api_key at rest (DB ciphertext != cleartext)
  - DELETE clears creds + sets enabled=False
  - POST /test calls upstream with the decrypted key + reports ok/fail
  - PUT never echoes cleartext key in its response
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from auth.utils import get_password_hash
from services.providers.crypto import decrypt_api_key


def _current_models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal

    return Organization, User, UserOrganization, SessionLocal


def _login_headers(client, username: str, password: str, org_slug: str | None = None):
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _seed_user(db, username: str, is_superuser: bool = False):
    _, User, _, _ = _current_models()
    user = User(
        email=f"{username}@example.com",
        username=username,
        hashed_password=get_password_hash("test123"),
        is_active=True,
        is_verified=True,
        is_superuser=is_superuser,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_org(db, slug: str):
    Organization, _, _, _ = _current_models()
    org = Organization(
        name=f"Org {slug}",
        slug=slug,
        is_active=True,
        integrations={},
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _seed_membership(db, user_id: int, org_id: int, role: str = "user"):
    _, _, UserOrganization, _ = _current_models()
    mem = UserOrganization(user_id=user_id, organization_id=org_id, role=role)
    db.add(mem)
    db.commit()
    return mem


def _unique(prefix: str) -> str:
    """Short unique slug/username for the test DB."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000",
        "http://169.254.169.254/latest/meta-data",
        "https://localhost:8443",
        "https://user:password@example.com",
        "https://example.com?redirect=http://127.0.0.1",
    ],
)
def test_integration_url_validation_blocks_private_and_credentialed_targets(url):
    from fastapi import HTTPException
    from api.integrations_org import _validated_integration_base_url

    with pytest.raises(HTTPException):
        _validated_integration_base_url(url)


def test_integration_url_validation_rejects_dns_to_private_address():
    from fastapi import HTTPException
    from api.integrations_org import _validated_integration_base_url

    with patch(
        "api.integrations_org.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("10.20.30.40", 443))],
    ):
        with pytest.raises(HTTPException, match="Private"):
            _validated_integration_base_url("https://integration.example.com")


def test_integration_url_validation_allows_public_https_and_named_services():
    from api.integrations_org import _validated_integration_base_url

    assert (
        _validated_integration_base_url("http://project-ops-backend/api/v1/")
        == "http://project-ops-backend/api/v1"
    )
    with patch(
        "api.integrations_org.socket.getaddrinfo",
        return_value=[(2, 1, 6, "", ("8.8.8.8", 443))],
    ):
        assert (
            _validated_integration_base_url("https://integration.example.com/v1/")
            == "https://integration.example.com/v1"
        )


class TestListMasksSecrets:
    def test_list_returns_all_five_integrations_with_masked_secrets(self, client):
        _, _, _, SessionLocal = _current_models()
        db = SessionLocal()
        try:
            u_slug = _unique("user")
            o_slug = _unique("org")
            user = _seed_user(db, u_slug)
            org = _seed_org(db, o_slug)
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, u_slug, "test123", o_slug)

            # Pre-seed an encrypted Brigade key via PUT so we know the
            # mask logic has something real to mask.
            put_resp = client.put(
                "/api/integrations/me/brigade",
                json={
                    "enabled": True,
                    "api_base_url": "http://unicorn-brigade",
                    "api_key": "super-secret-key-abcd1234",
                    "tenancy_mode": "per_org_graph",
                },
                headers=headers,
            )
            assert put_resp.status_code == 200, put_resp.text

            resp = client.get("/api/integrations/me", headers=headers)
            assert resp.status_code == 200
            rows = {r["key"]: r for r in resp.json()}
            assert set(rows.keys()) == {
                "brigade",
                "project_ops",
                "contact_ops",
                "accounting_ops",
                "stable",
            }
            brigade = rows["brigade"]
            assert brigade["enabled"] is True
            assert brigade["has_api_key"] is True
            assert brigade["api_key_last4"] == "1234"
            # No cleartext field anywhere in the response.
            assert "api_key" not in brigade
            assert "api_key_encrypted" not in brigade
        finally:
            db.rollback()
            db.close()


class TestPutRequiresAdmin:
    def test_non_admin_user_gets_403(self, client):
        _, _, _, SessionLocal = _current_models()
        db = SessionLocal()
        try:
            u_slug = _unique("plain")
            o_slug = _unique("plainorg")
            user = _seed_user(db, u_slug)
            org = _seed_org(db, o_slug)
            _seed_membership(db, user.id, org.id, "user")
            headers = _login_headers(client, u_slug, "test123", o_slug)

            resp = client.put(
                "/api/integrations/me/brigade",
                json={"enabled": True, "api_key": "k"},
                headers=headers,
            )
            assert resp.status_code == 403

            # GET, however, is fine for any org member (returns masked).
            list_resp = client.get("/api/integrations/me", headers=headers)
            assert list_resp.status_code == 200
        finally:
            db.rollback()
            db.close()

    def test_admin_can_save_and_secret_is_encrypted_at_rest(self, client):
        _, _, _, SessionLocal = _current_models()
        db = SessionLocal()
        try:
            u_slug = _unique("admin")
            o_slug = _unique("adminorg")
            user = _seed_user(db, u_slug)
            org = _seed_org(db, o_slug)
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, u_slug, "test123", o_slug)

            cleartext = "po-cleartext-token-9999"
            resp = client.put(
                "/api/integrations/me/project_ops",
                json={
                    "enabled": True,
                    "api_base_url": "http://project-ops-backend/api/v1",
                    "api_key": cleartext,
                    "default_project_number": "P-00055",
                },
                headers=headers,
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["enabled"] is True
            assert body["has_api_key"] is True
            assert body["api_key_last4"] == "9999"
            # Response NEVER includes the cleartext.
            assert "api_key" not in body
            assert cleartext not in str(body)

            # Verify ciphertext-at-rest by reading the row directly.
            db.refresh(org)
            stored = org.integrations["project_ops"]["api_key_encrypted"]
            assert stored != cleartext
            assert stored != ""
            # And it decrypts back to the original value.
            assert decrypt_api_key(stored) == cleartext
            # Extras round-tripped.
            assert org.integrations["project_ops"]["default_project_number"] == "P-00055"
        finally:
            db.rollback()
            db.close()


class TestDelete:
    def test_delete_clears_creds_and_disables(self, client):
        _, _, _, SessionLocal = _current_models()
        db = SessionLocal()
        try:
            u_slug = _unique("delete-admin")
            o_slug = _unique("delete-org")
            user = _seed_user(db, u_slug)
            org = _seed_org(db, o_slug)
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, u_slug, "test123", o_slug)

            # Seed it
            client.put(
                "/api/integrations/me/brigade",
                json={
                    "enabled": True,
                    "api_base_url": "http://unicorn-brigade",
                    "api_key": "k1234",
                },
                headers=headers,
            )

            resp = client.delete("/api/integrations/me/brigade", headers=headers)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["enabled"] is False
            assert body["has_api_key"] is False
            # api_base_url stays (admin can re-enable without re-entering URL).
            assert body["api_base_url"] == "http://unicorn-brigade"

            db.refresh(org)
            assert org.integrations["brigade"]["enabled"] is False
            assert org.integrations["brigade"]["api_key_encrypted"] == ""
        finally:
            db.rollback()
            db.close()


class TestProbe:
    def test_brigade_probe_uses_xapikey_header_and_reports_ok(self, client):
        _, _, _, SessionLocal = _current_models()
        db = SessionLocal()
        try:
            u_slug = _unique("probe-admin")
            o_slug = _unique("probe-org")
            user = _seed_user(db, u_slug)
            org = _seed_org(db, o_slug)
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, u_slug, "test123", o_slug)

            client.put(
                "/api/integrations/me/brigade",
                json={
                    "enabled": True,
                    "api_base_url": "http://unicorn-brigade",
                    "api_key": "brigade-key-zzzz",
                },
                headers=headers,
            )

            # Fake httpx response: 200 OK.
            class FakeResp:
                status_code = 200

                def json(self):
                    return {}

            fake_get = AsyncMock(return_value=FakeResp())
            with patch("api.integrations_org.httpx.AsyncClient") as ac:
                ac.return_value.__aenter__.return_value.get = fake_get
                ac.return_value.__aexit__.return_value = None
                resp = client.post(
                    "/api/integrations/me/brigade/test", headers=headers
                )
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"ok": True, "error": None}

            # Confirm the call used the X-API-Key header (not Bearer).
            args, kwargs = fake_get.call_args
            sent_headers = kwargs.get("headers") or (args[1] if len(args) > 1 else {})
            assert sent_headers.get("X-API-Key") == "brigade-key-zzzz"
            assert "Authorization" not in sent_headers
        finally:
            db.rollback()
            db.close()

    def test_project_ops_probe_uses_bearer_and_reports_fail_on_401(self, client):
        _, _, _, SessionLocal = _current_models()
        db = SessionLocal()
        try:
            u_slug = _unique("po-probe-admin")
            o_slug = _unique("po-probe-org")
            user = _seed_user(db, u_slug)
            org = _seed_org(db, o_slug)
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, u_slug, "test123", o_slug)

            client.put(
                "/api/integrations/me/project_ops",
                json={
                    "enabled": True,
                    "api_base_url": "http://project-ops-backend/api/v1",
                    "api_key": "po-key-zzzz",
                },
                headers=headers,
            )

            class FakeResp:
                status_code = 401

                def json(self):
                    return {}

            fake_get = AsyncMock(return_value=FakeResp())
            with patch("api.integrations_org.httpx.AsyncClient") as ac:
                ac.return_value.__aenter__.return_value.get = fake_get
                ac.return_value.__aexit__.return_value = None
                resp = client.post(
                    "/api/integrations/me/project_ops/test", headers=headers
                )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["ok"] is False
            assert "401" in body["error"] or "Authentication" in body["error"]

            _args, kwargs = fake_get.call_args
            sent_headers = kwargs.get("headers") or {}
            assert sent_headers.get("Authorization") == "Bearer po-key-zzzz"
        finally:
            db.rollback()
            db.close()

    def test_probe_refuses_when_disabled(self, client):
        _, _, _, SessionLocal = _current_models()
        db = SessionLocal()
        try:
            u_slug = _unique("dis-admin")
            o_slug = _unique("dis-org")
            user = _seed_user(db, u_slug)
            org = _seed_org(db, o_slug)
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, u_slug, "test123", o_slug)

            resp = client.post(
                "/api/integrations/me/brigade/test", headers=headers
            )
            assert resp.status_code == 200
            assert resp.json()["ok"] is False


            resp_invalid = client.post(
                "/api/integrations/me/totally_made_up/test", headers=headers
            )
            assert resp_invalid.status_code == 400
        finally:
            db.rollback()
            db.close()
