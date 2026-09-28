"""Phase 3G tests - Jira, Todoist, ClickUp, Notion, Confluence, Slack,
Teams, Email. Contract tested (HTTP boundary mocked); no real credentials
exist in this environment (same policy as Lyzr/Omi/Google Calendar)."""
from __future__ import annotations

import uuid

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


def _create_action_item(text="Follow up on the proposal", owner="Aaron") -> int:
    from database.database import SessionLocal
    from database.models import ActionItem, RecordingSession

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


def _configure_integration(client, headers, key: str, base_url: str, api_key: str, extra: dict = None):
    body = {"enabled": True, "api_base_url": base_url, "api_key": api_key}
    if extra:
        body.update(extra)
    resp = client.put(f"/api/integrations/me/{key}", headers=headers, json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Config CRUD (free via the existing generic org_config system)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["jira", "todoist", "clickup", "notion", "confluence", "slack", "teams", "email"])
def test_all_8_providers_are_valid_configurable_integrations(client, key):
    headers = _admin_bearer(client)
    resp = client.get(f"/api/integrations/me/{key}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["key"] == key
    assert resp.json()["enabled"] is False  # not configured yet


def test_unknown_provider_key_rejected(client):
    headers = _admin_bearer(client)
    resp = client.get("/api/integrations/me/not-a-real-provider", headers=headers)
    assert resp.status_code in (400, 404, 422)


# ---------------------------------------------------------------------------
# Not configured -> explicit failure, never fake success
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("provider,endpoint,body", [
    ("jira", "push", {"project_key": "PROJ"}),
    ("todoist", "push", {}),
    ("clickup", "push", {"list_id": "123"}),
    ("notion", "push", {"database_id": "abc"}),
    ("confluence", "push", {"space_key": "ENG"}),
])
def test_task_provider_not_configured_returns_explicit_failure(client, provider, endpoint, body):
    headers = _admin_bearer(client)
    item_id = _create_action_item()
    resp = client.post(f"/api/action-items/{item_id}/{endpoint}/{provider}", headers=headers, json=body)
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "not configured" in resp.json()["error"].lower()


def test_slack_not_configured_returns_explicit_failure(client):
    headers = _admin_bearer(client)
    item_id = _create_action_item()
    resp = client.post(f"/api/action-items/{item_id}/notify/slack", headers=headers, json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

def test_jira_issue_creation_success(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "jira", "https://acme.atlassian.net", "aaron@acme.com:fake-api-token")
    item_id = _create_action_item(text="Ship the migration")

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        assert url == "https://acme.atlassian.net/rest/api/3/issue"
        assert headers["Authorization"].startswith("Basic ")
        assert json["fields"]["project"]["key"] == "PROJ"
        assert json["fields"]["summary"] == "Ship the migration"
        return _FakeResponse(201, {"id": "10001", "key": "PROJ-42"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/push/jira", headers=headers, json={"project_key": "PROJ"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["ref"]["key"] == "PROJ-42"


def test_jira_requires_project_key(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "jira", "https://acme.atlassian.net", "a@b.com:tok")
    item_id = _create_action_item()
    resp = client.post(f"/api/action-items/{item_id}/push/jira", headers=headers, json={})
    assert resp.status_code == 422


def test_jira_api_failure_recorded_not_hidden(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "jira", "https://acme.atlassian.net", "a@b.com:bad-tok")
    item_id = _create_action_item()

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        return _FakeResponse(401, {}, text="unauthorized")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/push/jira", headers=headers, json={"project_key": "PROJ"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "401" in resp.json()["error"]


# ---------------------------------------------------------------------------
# Todoist / ClickUp / Notion / Confluence - one success test each
# ---------------------------------------------------------------------------

def test_todoist_task_creation_success(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "todoist", "https://api.todoist.com", "fake-todoist-token")
    item_id = _create_action_item(text="Review PR #42")

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        assert url == "https://api.todoist.com/rest/v2/tasks"
        assert headers["Authorization"] == "Bearer fake-todoist-token"
        return _FakeResponse(200, {"id": "999", "url": "https://todoist.com/task/999"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/push/todoist", headers=headers, json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["ref"]["id"] == "999"


def test_clickup_task_creation_success(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "clickup", "https://api.clickup.com", "fake-clickup-token")
    item_id = _create_action_item()

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        assert url == "https://api.clickup.com/api/v2/list/456/task"
        assert headers["Authorization"] == "fake-clickup-token"  # raw, no Bearer prefix
        return _FakeResponse(200, {"id": "task123", "url": "https://app.clickup.com/t/task123"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/push/clickup", headers=headers, json={"list_id": "456"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_notion_page_creation_success(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "notion", "https://api.notion.com", "secret_fake_notion_token")
    item_id = _create_action_item()

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        assert url == "https://api.notion.com/v1/pages"
        assert headers["Notion-Version"] == "2022-06-28"
        return _FakeResponse(200, {"id": "page-abc", "url": "https://notion.so/page-abc"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/push/notion", headers=headers, json={"database_id": "db-1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_confluence_page_creation_success(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "confluence", "https://acme.atlassian.net", "aaron@acme.com:fake-token")
    item_id = _create_action_item()

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        assert url == "https://acme.atlassian.net/wiki/rest/api/content"
        assert headers["Authorization"].startswith("Basic ")
        return _FakeResponse(200, {"id": "page-1", "_links": {"webui": "/wiki/spaces/ENG/pages/page-1"}})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/push/confluence", headers=headers, json={"space_key": "ENG"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# Slack / Teams (webhooks)
# ---------------------------------------------------------------------------

def test_slack_notification_success(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "slack", "https://hooks.slack.com/services/T00/B00/fakehook", "unused")
    item_id = _create_action_item(text="Escalate the outage")

    async def fake_post(self, url, json=None, **kwargs):
        assert url == "https://hooks.slack.com/services/T00/B00/fakehook"
        assert "Escalate the outage" in json["text"]
        return _FakeResponse(200, {})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/notify/slack", headers=headers, json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_teams_notification_success(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "teams", "https://acme.webhook.office.com/fakehook", "unused")
    item_id = _create_action_item()

    async def fake_post(self, url, json=None, **kwargs):
        assert json["@type"] == "MessageCard"
        return _FakeResponse(200, {})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/notify/teams", headers=headers, json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_slack_webhook_rejection_is_reported(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "slack", "https://hooks.slack.com/services/T00/B00/revoked", "unused")
    item_id = _create_action_item()

    async def fake_post(self, url, json=None, **kwargs):
        return _FakeResponse(404, {}, text="no_service")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    resp = client.post(f"/api/action-items/{item_id}/notify/slack", headers=headers, json={})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Email (real smtplib, socket-level mocked)
# ---------------------------------------------------------------------------

def test_email_send_success(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(
        client, headers, "email", "", "fake-smtp-password",
        extra={"smtp_host": "smtp.acme.com", "smtp_port": 587, "smtp_username": "notifier@acme.com", "smtp_from_address": "notifier@acme.com", "smtp_use_tls": True},
    )
    item_id = _create_action_item(text="Confirm the vendor contract")

    calls = {"login": None, "sendmail": None}

    class _FakeSMTP:
        def __init__(self, host, port, timeout=None):
            assert host == "smtp.acme.com"
            assert port == 587

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, username, password):
            calls["login"] = (username, password)

        def sendmail(self, from_addr, to_addrs, msg):
            calls["sendmail"] = (from_addr, to_addrs)

    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    resp = client.post(f"/api/action-items/{item_id}/notify/email", headers=headers, json={"to_address": "team@acme.com"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert calls["login"] == ("notifier@acme.com", "fake-smtp-password")
    assert calls["sendmail"][1] == ["team@acme.com"]


def test_email_requires_to_address(client):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "email", "", "pw", extra={"smtp_host": "smtp.acme.com", "smtp_username": "a@b.com"})
    item_id = _create_action_item()
    resp = client.post(f"/api/action-items/{item_id}/notify/email", headers=headers, json={})
    assert resp.status_code == 422


def test_email_smtp_connection_failure_reported_not_hidden(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "email", "", "pw", extra={"smtp_host": "smtp.acme.com", "smtp_username": "a@b.com"})
    item_id = _create_action_item()

    class _FailingSMTP:
        def __init__(self, host, port, timeout=None):
            import smtplib
            raise smtplib.SMTPConnectError(421, "cannot connect")

    monkeypatch.setattr("smtplib.SMTP", _FailingSMTP)
    resp = client.post(f"/api/action-items/{item_id}/notify/email", headers=headers, json={"to_address": "x@y.com"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ---------------------------------------------------------------------------
# Refs are recorded on the action item (traceability)
# ---------------------------------------------------------------------------

def test_successful_push_is_recorded_on_action_item(client, monkeypatch):
    headers = _admin_bearer(client)
    _configure_integration(client, headers, "todoist", "https://api.todoist.com", "tok")
    item_id = _create_action_item()

    async def fake_post(self, url, json=None, headers=None, **kwargs):
        return _FakeResponse(200, {"id": "555", "url": "https://todoist.com/task/555"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    client.post(f"/api/action-items/{item_id}/push/todoist", headers=headers, json={})

    from database.database import SessionLocal
    from database.models import ActionItem

    db = SessionLocal()
    item = db.query(ActionItem).filter(ActionItem.id == item_id).first()
    assert item.external_task_refs["todoist"]["id"] == "555"
    assert "synced_at" in item.external_task_refs["todoist"]
    db.close()


def test_unauthenticated_push_is_rejected(client):
    item_id = _create_action_item()
    resp = client.post(f"/api/action-items/{item_id}/push/jira", json={"project_key": "P"})
    assert resp.status_code in (401, 403)


def test_push_for_nonexistent_action_item_404s(client):
    headers = _admin_bearer(client)
    resp = client.post("/api/action-items/999999/push/jira", headers=headers, json={"project_key": "P"})
    assert resp.status_code == 404
