"""Tests for the customer-support contact endpoint (v3.21.0).

Covers:
  - Happy path: 200 + row inserted with subject + message.
  - Bad email shape: 400.
  - Missing subject / message: 400.
  - Rate limit: 4th request from the same email is 429.
  - Postmark inert when token unset (no exception, logs only).
"""

from __future__ import annotations

import pytest


def _models():
    from database.database import SessionLocal
    from database.models import SupportRequest
    return SupportRequest, SessionLocal


def _reset_support_state():
    from api import support as support_module
    support_module._INPROC_BUCKET.clear()
    return support_module


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("POSTMARK_API_TOKEN", raising=False)
    monkeypatch.delenv("POSTMARK_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("POSTMARK_FROM", raising=False)
    _reset_support_state()
    yield
    _reset_support_state()
    SupportRequest, SessionLocal = _models()
    db = SessionLocal()
    try:
        db.query(SupportRequest).delete()
        db.commit()
    finally:
        db.close()


def _payload(**overrides):
    body = {
        "name": "Alice",
        "email": "alice@example.com",
        "subject": "Help with recording",
        "message": "My mic stopped working halfway through a meeting.",
    }
    body.update(overrides)
    return body


def test_contact_inserts_row(client):
    resp = client.post("/api/support/contact", json=_payload())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    SupportRequest, SessionLocal = _models()
    db = SessionLocal()
    try:
        rows = db.query(SupportRequest).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.email == "alice@example.com"
        assert row.name == "Alice"
        assert row.subject == "Help with recording"
        assert "mic stopped" in row.message
        # Unauthed -> no user_id stamp.
        assert row.user_id is None
    finally:
        db.close()


def test_contact_bad_email_is_400(client):
    resp = client.post(
        "/api/support/contact",
        json=_payload(email="not-an-email"),
    )
    assert resp.status_code == 400


def test_contact_missing_subject_is_400(client):
    resp = client.post(
        "/api/support/contact",
        json=_payload(subject=""),
    )
    assert resp.status_code == 400


def test_contact_missing_message_is_400(client):
    resp = client.post(
        "/api/support/contact",
        json=_payload(message=""),
    )
    assert resp.status_code == 400


def test_contact_rate_limit_per_email(client):
    # 3 / hour per email; the 4th must 429.
    for i in range(3):
        resp = client.post(
            "/api/support/contact",
            json=_payload(subject=f"Subject {i}"),
        )
        assert resp.status_code == 200, f"expected 200 on request {i}, got {resp.status_code}: {resp.text}"
    resp = client.post(
        "/api/support/contact",
        json=_payload(subject="Subject 4"),
    )
    assert resp.status_code == 429


def test_contact_postmark_inert_when_unconfigured(client, monkeypatch):
    # Sanity: with no POSTMARK_* env, the endpoint still returns 200 and
    # does not raise. We don't have a Postmark sandbox here so we just
    # exercise the no-token path.
    resp = client.post("/api/support/contact", json=_payload())
    assert resp.status_code == 200
