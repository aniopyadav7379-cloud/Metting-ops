"""Tests for the Keycloak identity-erasure helper (services.keycloak_admin)
and its wiring into account self-delete (DELETE /api/auth/me).

All offline: HTTP is mocked by monkeypatching the module's ``_client`` factory
seam with an ``httpx.AsyncClient`` bound to ``httpx.MockTransport`` (no respx
dependency, no sockets). Covers:

  unit (helper):
    (i)   configured + KC user found  -> token POST, exact-email lookup GET,
          DELETE called with the KC id + Bearer header, returns True
    (ii)  configured + user not found -> False, NO delete call, no exception
    (iii) NOT configured              -> False, zero HTTP calls
    (iv)  KC 500                      -> swallowed, False
    plus: sub/UUID form skips the lookup; a non-matching lookup row is never
          deleted (destructive call requires an exact email match)

  endpoint (DELETE /api/auth/me):
    not configured -> 200, zero HTTP; configured happy path -> 200 + the
    seeded user's email drives the KC delete; KC 500 -> still 200.

Runs on the SQLite fixture (tests/conftest.py), same idioms as
test_account_self_delete.py.
"""
from __future__ import annotations

import asyncio

import httpx

from auth.utils import get_password_hash

KC_ENV = (
    "KEYCLOAK_ADMIN_BASE",
    "KEYCLOAK_URL",
    "KEYCLOAK_REALM",
    "KEYCLOAK_ADMIN_CLIENT_ID",
    "KEYCLOAK_ADMIN_CLIENT_SECRET",
)


def _clear_kc_env(monkeypatch):
    for name in KC_ENV:
        monkeypatch.delenv(name, raising=False)


def _set_kc_env(monkeypatch, base: str = "https://kc.test"):
    monkeypatch.setenv("KEYCLOAK_ADMIN_BASE", base)
    monkeypatch.setenv("KEYCLOAK_REALM", "uchub")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meeting-ops-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "test-secret")


def _install_transport(monkeypatch, handler):
    """Point keycloak_admin._client at an offline MockTransport client and
    return the list the handler appends (method, url) tuples to."""
    import services.keycloak_admin as keycloak_admin

    calls: list[tuple[str, str]] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return handler(request)

    monkeypatch.setattr(
        keycloak_admin,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(recording_handler)),
    )
    return calls


def _forbid_http(monkeypatch):
    """Trap any HTTP-client construction. Returns the attempt list — callers
    MUST assert it stays empty (the helper swallows exceptions by design, so
    merely raising in the factory could be silently eaten)."""
    import services.keycloak_admin as keycloak_admin

    attempts: list[int] = []

    def _boom():
        attempts.append(1)
        raise AssertionError("keycloak_admin made an HTTP client while unconfigured")

    monkeypatch.setattr(keycloak_admin, "_client", _boom)
    return attempts


def _kc_handler(
    *,
    found_id: str | None,
    found_email: str | None = None,
    token_status: int = 200,
):
    """Canned Keycloak: token endpoint, exact-email user lookup, user delete."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/realms/uchub/protocol/openid-connect/token":
            assert request.method == "POST"
            body = request.content.decode()
            assert "grant_type=client_credentials" in body
            assert "client_id=meeting-ops-admin" in body
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "boom"})
            return httpx.Response(
                200, json={"access_token": "kc-admin-token", "expires_in": 60}
            )
        # Admin API requires the bearer from the token call above.
        assert request.headers.get("Authorization") == "Bearer kc-admin-token"
        if request.method == "GET" and path == "/admin/realms/uchub/users":
            assert request.url.params.get("exact") == "true"
            assert request.url.params.get("email")
            if found_id is None:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": found_id,
                        "email": found_email or request.url.params["email"],
                    }
                ],
            )
        if request.method == "DELETE" and path.startswith("/admin/realms/uchub/users/"):
            return httpx.Response(204)
        raise AssertionError(f"unexpected KC call: {request.method} {request.url}")

    return handler


# ---------------------------------------------------------------------------
# unit: the helper itself
# ---------------------------------------------------------------------------

def test_configured_user_found_token_lookup_delete_true(monkeypatch):
    from services.keycloak_admin import delete_keycloak_user

    _set_kc_env(monkeypatch)
    calls = _install_transport(monkeypatch, _kc_handler(found_id="kc-uuid-1"))

    assert asyncio.run(delete_keycloak_user("doomed@example.com")) is True

    assert [m for m, _ in calls] == ["POST", "GET", "DELETE"]
    assert calls[0][1] == "https://kc.test/realms/uchub/protocol/openid-connect/token"
    assert calls[1][1].startswith("https://kc.test/admin/realms/uchub/users?")
    assert "exact=true" in calls[1][1]
    assert calls[2][1] == "https://kc.test/admin/realms/uchub/users/kc-uuid-1"


def test_configured_user_not_found_false_no_delete(monkeypatch):
    from services.keycloak_admin import delete_keycloak_user

    _set_kc_env(monkeypatch)
    calls = _install_transport(monkeypatch, _kc_handler(found_id=None))

    assert asyncio.run(delete_keycloak_user("ghost@example.com")) is False
    assert [m for m, _ in calls] == ["POST", "GET"]  # token + lookup, no DELETE


def test_not_configured_false_and_zero_http(monkeypatch):
    from services.keycloak_admin import delete_keycloak_user, is_configured

    _clear_kc_env(monkeypatch)
    attempts = _forbid_http(monkeypatch)

    assert is_configured() is False
    assert asyncio.run(delete_keycloak_user("whoever@example.com")) is False
    assert attempts == []


def test_partially_configured_counts_as_unconfigured(monkeypatch):
    """Base URL alone (the customer compose already sets KEYCLOAK_URL) must
    NOT activate the feature — the admin client id+secret are required."""
    from services.keycloak_admin import delete_keycloak_user

    _clear_kc_env(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc.test")
    monkeypatch.setenv("KEYCLOAK_REALM", "uchub")
    attempts = _forbid_http(monkeypatch)

    assert asyncio.run(delete_keycloak_user("whoever@example.com")) is False
    assert attempts == []


def test_kc_500_swallowed_returns_false(monkeypatch):
    from services.keycloak_admin import delete_keycloak_user

    _set_kc_env(monkeypatch)
    calls = _install_transport(
        monkeypatch, _kc_handler(found_id="kc-uuid-1", token_status=500)
    )

    # raise_for_status() fires inside, must be swallowed -> False, no raise.
    assert asyncio.run(delete_keycloak_user("doomed@example.com")) is False
    assert [m for m, _ in calls] == ["POST"]


def test_sub_uuid_form_deletes_directly_without_lookup(monkeypatch):
    from services.keycloak_admin import delete_keycloak_user

    _set_kc_env(monkeypatch)
    calls = _install_transport(monkeypatch, _kc_handler(found_id=None))

    sub = "3f2a77f1-0000-4d62-9a51-0123456789ab"
    assert asyncio.run(delete_keycloak_user(sub)) is True
    assert [m for m, _ in calls] == ["POST", "DELETE"]
    assert calls[1][1] == f"https://kc.test/admin/realms/uchub/users/{sub}"


def test_lookup_row_with_mismatched_email_is_never_deleted(monkeypatch):
    """Defense-in-depth: a destructive delete requires the returned row's
    email to actually equal the requested one (case-insensitive)."""
    from services.keycloak_admin import delete_keycloak_user

    _set_kc_env(monkeypatch)
    calls = _install_transport(
        monkeypatch,
        _kc_handler(found_id="kc-uuid-9", found_email="someone.else@example.com"),
    )

    assert asyncio.run(delete_keycloak_user("doomed@example.com")) is False
    assert [m for m, _ in calls] == ["POST", "GET"]


def test_fallback_to_existing_keycloak_url_env(monkeypatch):
    """KEYCLOAK_ADMIN_BASE unset but KEYCLOAK_URL set (the deployed shape on
    the customer node) + the new client creds -> feature active on that URL."""
    from services.keycloak_admin import delete_keycloak_user

    _clear_kc_env(monkeypatch)
    monkeypatch.setenv("KEYCLOAK_URL", "https://kc-public.test/")  # trailing /
    monkeypatch.setenv("KEYCLOAK_REALM", "uchub")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meeting-ops-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "test-secret")
    calls = _install_transport(monkeypatch, _kc_handler(found_id="kc-uuid-2"))

    assert asyncio.run(delete_keycloak_user("doomed@example.com")) is True
    assert calls[0][1] == (
        "https://kc-public.test/realms/uchub/protocol/openid-connect/token"
    )


# ---------------------------------------------------------------------------
# endpoint: DELETE /api/auth/me wiring (same seed/login idioms as
# test_account_self_delete.py; users here have no sessions, so the
# Qdrant/media purge loop is a no-op and needs no mocking)
# ---------------------------------------------------------------------------

def _seed_user_org(*, slug: str, username: str):
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(
                name=slug.replace("-", " ").title(), slug=slug, is_active=True
            )
            db.add(org)
            db.commit()
            db.refresh(org)

        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                hashed_password=get_password_hash("admin123"),
                is_active=True,
                is_verified=True,
                tier="pro",
            )
            db.add(user)
            db.commit()
            db.refresh(user)

        if not (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.organization_id == org.id,
            )
            .first()
        ):
            db.add(
                UserOrganization(user_id=user.id, organization_id=org.id, role="admin")
            )
            db.commit()

        db.refresh(org)
        db.refresh(user)
        return org, user
    finally:
        db.close()


def _login_headers(client, username: str, org_slug: str):
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": "admin123"}
    )
    assert resp.status_code == 200, resp.text
    return {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-MeetingOps-Org": org_slug,
    }


def _assert_user_gone(user_id: int):
    from auth.models import User
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        assert db.query(User).filter(User.id == user_id).first() is None
    finally:
        db.close()


def test_endpoint_unconfigured_deletes_account_with_zero_http(client, monkeypatch):
    org, user = _seed_user_org(slug="kcdel-unconf", username="kcdel_unconf_user")
    uid = user.id
    _clear_kc_env(monkeypatch)
    attempts = _forbid_http(monkeypatch)

    headers = _login_headers(client, "kcdel_unconf_user", org.slug)
    resp = client.delete("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    _assert_user_gone(uid)
    assert attempts == []  # config-gated: no KC client was even constructed


def test_endpoint_configured_passes_email_and_deletes_identity(client, monkeypatch):
    org, user = _seed_user_org(slug="kcdel-happy", username="kcdel_happy_user")
    uid, email = user.id, user.email
    _set_kc_env(monkeypatch)
    calls = _install_transport(monkeypatch, _kc_handler(found_id="kc-uuid-7"))

    headers = _login_headers(client, "kcdel_happy_user", org.slug)
    resp = client.delete("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text
    _assert_user_gone(uid)

    # The endpoint drove the full KC flow with the (pre-delete) email.
    assert [m for m, _ in calls] == ["POST", "GET", "DELETE"]
    assert f"email={email.replace('@', '%40')}" in calls[1][1]
    assert calls[2][1].endswith("/admin/realms/uchub/users/kc-uuid-7")


def test_endpoint_kc_500_still_returns_200(client, monkeypatch):
    org, user = _seed_user_org(slug="kcdel-kcdown", username="kcdel_kcdown_user")
    uid = user.id
    _set_kc_env(monkeypatch)
    calls = _install_transport(
        monkeypatch, _kc_handler(found_id="kc-uuid-8", token_status=500)
    )

    headers = _login_headers(client, "kcdel_kcdown_user", org.slug)
    resp = client.delete("/api/auth/me", headers=headers)
    assert resp.status_code == 200, resp.text  # KC failure never fails erasure
    _assert_user_gone(uid)
    assert [m for m, _ in calls] == ["POST"]  # attempted, failed, swallowed
