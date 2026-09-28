"""Tests for api/google_calendar.py + services/integrations/google_calendar.py.

Per the credential policy: real OAuth code against Google's documented
contracts, HTTP boundary mocked (no live GOOGLE_OAUTH_CLIENT_ID/SECRET
exist anywhere in this environment - confirmed the same way as Lyzr/Omi in
Phase 1/2). These are CONTRACT TESTED, not LIVE VERIFIED - that distinction
is preserved in the final report, not blurred here.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


class _FakeResponse:
    def __init__(self, status_code, json_data, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or str(json_data)

    def json(self):
        return self._json


@pytest.fixture(autouse=True)
def _google_env(monkeypatch):
    monkeypatch.setattr("services.integrations.google_calendar.GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr("services.integrations.google_calendar.GOOGLE_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setattr("services.integrations.google_calendar.GOOGLE_OAUTH_REDIRECT_URI", "https://app.example.com/api/integrations/google-calendar/callback")


def test_authorize_returns_503_when_not_configured(client, monkeypatch):
    monkeypatch.setattr("services.integrations.google_calendar.GOOGLE_OAUTH_CLIENT_ID", "")
    headers = _admin_bearer(client)
    resp = client.get("/api/integrations/google-calendar/authorize", headers=headers)
    assert resp.status_code == 503


def test_status_reports_not_configured_and_not_connected_by_default(client, monkeypatch):
    monkeypatch.setattr("services.integrations.google_calendar.GOOGLE_OAUTH_CLIENT_ID", "")
    headers = _admin_bearer(client)
    resp = client.get("/api/integrations/google-calendar/status", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert body["connected"] is False


def test_authorize_builds_correct_google_oauth_url(client):
    headers = _admin_bearer(client)
    resp = client.get("/api/integrations/google-calendar/authorize", headers=headers)
    assert resp.status_code == 200, resp.text
    url = resp.json()["authorization_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=test-client-id" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "calendar.events" in url
    assert "state=" in url


def test_authorize_requires_authentication(client):
    resp = client.get("/api/integrations/google-calendar/authorize")
    assert resp.status_code in (401, 403)


def test_callback_rejects_unknown_state(client):
    resp = client.get("/api/integrations/google-calendar/callback?code=abc&state=never-issued")
    assert resp.status_code == 400


def test_full_oauth_round_trip_stores_encrypted_tokens(client, monkeypatch):
    headers = _admin_bearer(client)
    auth_resp = client.get("/api/integrations/google-calendar/authorize", headers=headers)
    from urllib.parse import urlparse, parse_qs

    state = parse_qs(urlparse(auth_resp.json()["authorization_url"]).query)["state"][0]

    async def fake_post(self, url, data=None, headers=None, **kwargs):
        assert url == "https://oauth2.googleapis.com/token"
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "real-auth-code"
        return _FakeResponse(200, {"access_token": "fake-access-token", "refresh_token": "fake-refresh-token", "expires_in": 3600, "scope": "https://www.googleapis.com/auth/calendar.events"})

    async def fake_get(self, url, headers=None, **kwargs):
        assert url == "https://www.googleapis.com/oauth2/v2/userinfo"
        assert headers["Authorization"] == "Bearer fake-access-token"
        return _FakeResponse(200, {"email": "admin@example.com"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    resp = client.get(f"/api/integrations/google-calendar/callback?code=real-auth-code&state={state}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["google_email"] == "admin@example.com"

    status = client.get("/api/integrations/google-calendar/status", headers=headers)
    assert status.json()["connected"] is True
    assert status.json()["google_email"] == "admin@example.com"

    from database.database import SessionLocal
    from database.models import GoogleCalendarIntegration

    db = SessionLocal()
    row = db.query(GoogleCalendarIntegration).filter(GoogleCalendarIntegration.organization_id == 1).first()
    assert row.access_token_encrypted != "fake-access-token"
    assert row.refresh_token_encrypted != "fake-refresh-token"
    from services.providers.crypto import decrypt_api_key

    assert decrypt_api_key(row.access_token_encrypted) == "fake-access-token"
    db.close()


def test_state_is_single_use(client, monkeypatch):
    headers = _admin_bearer(client)
    auth_resp = client.get("/api/integrations/google-calendar/authorize", headers=headers)
    from urllib.parse import urlparse, parse_qs

    state = parse_qs(urlparse(auth_resp.json()["authorization_url"]).query)["state"][0]

    async def fake_post(self, url, data=None, headers=None, **kwargs):
        return _FakeResponse(200, {"access_token": "t", "refresh_token": "r", "expires_in": 3600})

    async def fake_get(self, url, headers=None, **kwargs):
        return _FakeResponse(200, {"email": "a@b.com"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    first = client.get(f"/api/integrations/google-calendar/callback?code=x&state={state}")
    assert first.status_code == 200
    second = client.get(f"/api/integrations/google-calendar/callback?code=x&state={state}")
    assert second.status_code == 400


def test_google_rejecting_the_code_surfaces_as_502_not_fake_success(client, monkeypatch):
    headers = _admin_bearer(client)
    auth_resp = client.get("/api/integrations/google-calendar/authorize", headers=headers)
    from urllib.parse import urlparse, parse_qs

    state = parse_qs(urlparse(auth_resp.json()["authorization_url"]).query)["state"][0]

    async def fake_post(self, url, data=None, headers=None, **kwargs):
        return _FakeResponse(400, {"error": "invalid_grant"}, text="invalid_grant")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.get(f"/api/integrations/google-calendar/callback?code=bad-code&state={state}")
    assert resp.status_code == 502


def _connect(client, headers, monkeypatch, access_expires_in=3600):
    auth_resp = client.get("/api/integrations/google-calendar/authorize", headers=headers)
    from urllib.parse import urlparse, parse_qs

    state = parse_qs(urlparse(auth_resp.json()["authorization_url"]).query)["state"][0]

    async def fake_post(self, url, data=None, headers=None, **kwargs):
        return _FakeResponse(200, {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": access_expires_in})

    async def fake_get(self, url, headers=None, **kwargs):
        return _FakeResponse(200, {"email": "admin@example.com"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    resp = client.get(f"/api/integrations/google-calendar/callback?code=c&state={state}")
    assert resp.status_code == 200, resp.text


def test_disconnect_clears_tokens(client, monkeypatch):
    headers = _admin_bearer(client)
    _connect(client, headers, monkeypatch)

    resp = client.post("/api/integrations/google-calendar/disconnect", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["was_connected"] is True

    status = client.get("/api/integrations/google-calendar/status", headers=headers)
    assert status.json()["connected"] is False

    from database.database import SessionLocal
    from database.models import GoogleCalendarIntegration

    db = SessionLocal()
    row = db.query(GoogleCalendarIntegration).filter(GoogleCalendarIntegration.organization_id == 1).first()
    assert row.access_token_encrypted is None
    assert row.refresh_token_encrypted is None
    db.close()


def test_disconnect_when_never_connected_is_a_no_op(client):
    headers = _admin_bearer(client)
    # Other tests in this file may have already connected+disconnected this
    # same admin/org pair (shared test DB) — delete any prior row so this
    # test genuinely exercises the "never connected" case, not leftover
    # state from test ordering.
    from database.database import SessionLocal
    from database.models import GoogleCalendarIntegration
    from auth.models import User

    db = SessionLocal()
    admin = db.query(User).filter(User.username == "admin").first()
    db.query(GoogleCalendarIntegration).filter(GoogleCalendarIntegration.organization_id == 1, GoogleCalendarIntegration.user_id == admin.id).delete()
    db.commit()
    db.close()

    resp = client.post("/api/integrations/google-calendar/disconnect", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["was_connected"] is False


def _create_action_item(text="Follow up with legal", owner="Aaron") -> int:
    from database.database import SessionLocal
    from database.models import ActionItem, RecordingSession
    import uuid

    db = SessionLocal()
    try:
        session = RecordingSession(session_id=str(uuid.uuid4()), name="Meeting", organization_id=1, status="completed")
        db.add(session)
        db.flush()
        item = ActionItem(session_id=session.id, organization_id=1, text=text, owner=owner)
        db.add(item)
        db.commit()
        db.refresh(item)
        return item.id
    finally:
        db.close()


def test_create_event_requires_connection_first(client):
    headers = _admin_bearer(client)
    item_id = _create_action_item()
    resp = client.post(f"/api/integrations/google-calendar/action-items/{item_id}/create-event", headers=headers, json={})
    assert resp.status_code == 400


def test_create_event_from_action_item_succeeds(client, monkeypatch):
    headers = _admin_bearer(client)
    _connect(client, headers, monkeypatch)
    item_id = _create_action_item(text="Ship the migration", owner="Shafen")

    async def fake_post(self, url, json=None, headers=None, data=None, **kwargs):
        if "calendar/v3" in url:
            assert headers["Authorization"] == "Bearer access-1"
            assert json["summary"] == "Ship the migration"
            return _FakeResponse(200, {"id": "google-event-123", "htmlLink": "https://calendar.google.com/event?eid=x"})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    resp = client.post(f"/api/integrations/google-calendar/action-items/{item_id}/create-event", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["google_event_id"] == "google-event-123"

    from database.database import SessionLocal
    from database.models import ActionItem

    db = SessionLocal()
    item = db.query(ActionItem).filter(ActionItem.id == item_id).first()
    assert item.google_calendar_event_id == "google-event-123"
    assert item.google_calendar_link_state == "linked"
    db.close()


def test_create_event_refreshes_expired_access_token(client, monkeypatch):
    headers = _admin_bearer(client)
    _connect(client, headers, monkeypatch, access_expires_in=-10)
    item_id = _create_action_item()

    calls = {"refresh": 0, "event": 0}

    async def fake_post(self, url, json=None, data=None, headers=None, **kwargs):
        if url == "https://oauth2.googleapis.com/token":
            calls["refresh"] += 1
            assert data["grant_type"] == "refresh_token"
            assert data["refresh_token"] == "refresh-1"
            return _FakeResponse(200, {"access_token": "refreshed-access", "expires_in": 3600})
        if "calendar/v3" in url:
            calls["event"] += 1
            assert headers["Authorization"] == "Bearer refreshed-access"
            return _FakeResponse(200, {"id": "evt-after-refresh"})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/integrations/google-calendar/action-items/{item_id}/create-event", headers=headers, json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls["refresh"] == 1
    assert calls["event"] == 1


def test_calendar_event_creation_failure_is_recorded_not_hidden(client, monkeypatch):
    headers = _admin_bearer(client)
    _connect(client, headers, monkeypatch)
    item_id = _create_action_item()

    async def fake_post(self, url, json=None, headers=None, data=None, **kwargs):
        if "calendar/v3" in url:
            return _FakeResponse(403, {"error": "insufficient_permissions"}, text="insufficient_permissions")
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/integrations/google-calendar/action-items/{item_id}/create-event", headers=headers, json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "insufficient_permissions" in body["error"]

    from database.database import SessionLocal
    from database.models import ActionItem

    db = SessionLocal()
    item = db.query(ActionItem).filter(ActionItem.id == item_id).first()
    assert item.google_calendar_link_state == "failed"
    db.close()


def test_create_event_for_nonexistent_action_item_404s(client, monkeypatch):
    headers = _admin_bearer(client)
    _connect(client, headers, monkeypatch)
    resp = client.post("/api/integrations/google-calendar/action-items/999999/create-event", headers=headers, json={})
    assert resp.status_code == 404
