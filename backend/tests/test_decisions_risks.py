"""Tests for api/decisions_risks.py — dedicated, sourced Decisions and
Risks/Open-Questions views (Phase 3 modules H, I)."""
from __future__ import annotations

import uuid

import pytest


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_session_with_insights(ai_insights: dict, title: str = "Architecture Review") -> str:
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    try:
        s = RecordingSession(
            session_id=str(uuid.uuid4()),
            name=title,
            title=title,
            organization_id=1,
            status="completed",
            ai_insights=ai_insights,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.session_id
    finally:
        db.close()


def test_decisions_are_flattened_with_source_attribution(client):
    session_id = _create_session_with_insights(
        {"key_decisions": ["Ship the migration on Friday", "Use Postgres over MySQL"], "follow_ups": []},
        title="Migration Planning",
    )
    resp = client.get("/api/decisions", headers=_admin_bearer(client))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    texts = [d["text"] for d in body]
    assert "Ship the migration on Friday" in texts
    assert "Use Postgres over MySQL" in texts
    matching = [d for d in body if d["session_id"] == session_id]
    assert all(d["session_title"] == "Migration Planning" for d in matching)


def test_risks_are_flattened_with_source_attribution(client):
    session_id = _create_session_with_insights(
        {"key_decisions": [], "follow_ups": ["Vendor contract still unsigned", "Load testing not yet scheduled"]},
        title="Launch Readiness",
    )
    resp = client.get("/api/risks", headers=_admin_bearer(client))
    assert resp.status_code == 200, resp.text
    texts = [r["text"] for r in resp.json()]
    assert "Vendor contract still unsigned" in texts
    assert "Load testing not yet scheduled" in texts


def test_decisions_filterable_by_session(client):
    s1 = _create_session_with_insights({"key_decisions": ["Decision A"], "follow_ups": []}, title="Meeting A")
    s2 = _create_session_with_insights({"key_decisions": ["Decision B"], "follow_ups": []}, title="Meeting B")

    resp = client.get(f"/api/decisions?session_id={s1}", headers=_admin_bearer(client))
    assert resp.status_code == 200
    texts = [d["text"] for d in resp.json()]
    assert "Decision A" in texts
    assert "Decision B" not in texts


def test_decisions_endpoint_requires_auth(client):
    resp = client.get("/api/decisions")
    assert resp.status_code in (401, 403)


def test_sessions_without_ai_insights_are_skipped_not_errored(client):
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    db.add(RecordingSession(session_id=str(uuid.uuid4()), name="No insights yet", organization_id=1, status="processing"))
    db.commit()
    db.close()

    resp = client.get("/api/decisions", headers=_admin_bearer(client))
    assert resp.status_code == 200  # never a 500 from a null/missing field
