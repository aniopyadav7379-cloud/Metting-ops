"""Tests for the public invite-only landing endpoint.

Covers:
  - Happy path: 200 + row inserted.
  - Duplicate email: still 200, no second row, Postmark not called.
  - Bad email shape: 400.
  - Rate limit: 6th request from the same IP is 429.
  - Postmark inert when token unset (no exception, logs only).
"""

from __future__ import annotations

import pytest


def _models():
    from database.database import SessionLocal
    from database.models import InviteRequest
    return InviteRequest, SessionLocal


def _reset_landing_state():
    """Clear the in-process rate-limit bucket between tests so a noisy
    earlier test doesn't push later ones into 429."""
    from api import landing as landing_module
    landing_module._INPROC_BUCKET.clear()
    # Force the in-proc fallback path by ensuring no REDIS_URL leaks
    # in from the shell.
    return landing_module


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)
    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("POSTMARK_FROM", raising=False)
    _reset_landing_state()
    yield
    _reset_landing_state()
    # Best-effort wipe of inserted rows so the test DB stays small.
    InviteRequest, SessionLocal = _models()
    db = SessionLocal()
    try:
        db.query(InviteRequest).delete()
        db.commit()
    finally:
        db.close()


def test_invite_request_inserts_row(client):
    resp = client.post(
        "/api/landing/invite-request",
        json={"email": "alice@example.com"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    InviteRequest, SessionLocal = _models()
    db = SessionLocal()
    try:
        rows = db.query(InviteRequest).all()
        assert len(rows) == 1
        assert rows[0].email == "alice@example.com"
        assert rows[0].processed_at is None
    finally:
        db.close()


def test_invite_request_is_idempotent_on_duplicate(client, monkeypatch):
    """A second POST with the same email returns 200 and does NOT
    insert a second row OR trigger a second Postmark send."""
    calls: list[str] = []

    def fake_notify(email: str) -> None:
        calls.append(email)

    monkeypatch.setattr("api.landing._notify_invite_request", fake_notify)

    r1 = client.post("/api/landing/invite-request", json={"email": "bob@example.com"})
    assert r1.status_code == 200
    r2 = client.post("/api/landing/invite-request", json={"email": "bob@example.com"})
    assert r2.status_code == 200

    InviteRequest, SessionLocal = _models()
    db = SessionLocal()
    try:
        assert db.query(InviteRequest).filter_by(email="bob@example.com").count() == 1
    finally:
        db.close()

    # First call notifies; duplicate does not.
    assert calls == ["bob@example.com"]


def test_invite_request_rejects_bad_email(client):
    resp = client.post(
        "/api/landing/invite-request",
        json={"email": "not-an-email"},
    )
    assert resp.status_code == 400


def test_invite_request_normalizes_email(client):
    """Casing + surrounding whitespace must not let the same address
    bypass the UNIQUE constraint."""
    r1 = client.post(
        "/api/landing/invite-request",
        json={"email": "Carol@Example.com"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/landing/invite-request",
        json={"email": "  carol@example.com  "},
    )
    assert r2.status_code == 200

    InviteRequest, SessionLocal = _models()
    db = SessionLocal()
    try:
        assert db.query(InviteRequest).filter_by(email="carol@example.com").count() == 1
    finally:
        db.close()


def test_invite_request_rate_limit_hits_at_6th_call(client):
    """5 fresh emails go through; the 6th from the same IP is 429.
    Uses the in-process bucket since REDIS_URL is unset in this test.
    Emails differ so the DB-side dedup doesn't mask the rate-limit
    behavior we're trying to exercise."""
    for i in range(5):
        resp = client.post(
            "/api/landing/invite-request",
            json={"email": f"user{i}@example.com"},
        )
        assert resp.status_code == 200, f"call {i} got {resp.status_code}: {resp.text}"

    resp = client.post(
        "/api/landing/invite-request",
        json={"email": "user5@example.com"},
    )
    assert resp.status_code == 429


def test_postmark_inert_when_token_unset(client, caplog):
    """With no POSTMARK_API_TOKEN / POSTMARK_SERVER_TOKEN configured,
    the endpoint must succeed, log that notification is skipped, and
    not raise."""
    import logging
    caplog.set_level(logging.INFO, logger="api.landing")
    resp = client.post(
        "/api/landing/invite-request",
        json={"email": "inert@example.com"},
    )
    assert resp.status_code == 200
    # The notify path should have logged a "postmark not configured"
    # message; we don't pin the exact text but we do confirm at least
    # one INFO landed from the api.landing logger.
    assert any("postmark" in rec.message.lower() for rec in caplog.records)
