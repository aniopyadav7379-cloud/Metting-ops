"""Tests for api/omi_webhooks.py — Omi Real-Time Transcript + Memory Creation
webhooks. Mirrors the auth/idempotency test shape of test_stable_ingest.py,
adapted for the path-carried-token auth Omi's platform constraints require.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from database.database import SessionLocal
from database.models import RecordingSession, Transcription


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _omi_token(client, org_slug: str = "magic-unicorn") -> str:
    created = client.post(
        "/api/auth/pats",
        headers=_admin_bearer(client),
        json={
            "name": "Omi bridge",
            "scope": "omi.transcript.ingest",
            "organization_slug": org_slug,
            "expires_in_days": 90,
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["plaintext"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_realtime_requires_valid_token(client):
    resp = client.post(
        "/api/v1/integrations/omi/mops_pat_totally-bogus/realtime-transcript",
        json={"session_id": "abc", "segments": [{"text": "hi", "speaker": "A"}]},
    )
    assert resp.status_code == 401


def test_realtime_rejects_generic_user_scope_token(client):
    created = client.post(
        "/api/auth/pats",
        headers=_admin_bearer(client),
        json={"name": "just a user pat", "scope": "user"},
    )
    assert created.status_code == 201, created.text
    token = created.json()["plaintext"]
    resp = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={"session_id": "abc", "segments": [{"text": "hi", "speaker": "A"}]},
    )
    assert resp.status_code == 403


def test_realtime_rejects_stable_scope_token(client):
    """A token scoped for a *different* integration must not work here —
    scopes are not interchangeable."""
    created = client.post(
        "/api/auth/pats",
        headers=_admin_bearer(client),
        json={
            "name": "stable bridge",
            "scope": "stable.transcript.ingest",
            "organization_slug": "magic-unicorn",
            "expires_in_days": 30,
        },
    )
    assert created.status_code == 201
    token = created.json()["plaintext"]
    resp = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={"session_id": "abc", "segments": [{"text": "hi", "speaker": "A"}]},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Real-time transcript: creation, append, idempotency, dedup
# ---------------------------------------------------------------------------

def test_realtime_transcript_creates_live_session_and_appends(client):
    token = _omi_token(client)

    r1 = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={
            "session_id": "omi-sess-1",
            "segments": [{"text": "Let's start the standup.", "speaker": "Aaron", "is_user": True, "start": 0.0, "end": 2.0}],
        },
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["segments_new"] == 1

    r2 = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={
            "session_id": "omi-sess-1",
            "segments": [{"text": "I'll take the backend task.", "speaker": "Shafen", "start": 2.0, "end": 4.0}],
        },
    )
    assert r2.status_code == 200
    assert r2.json()["session_id"] == body1["session_id"]
    assert r2.json()["segments_new"] == 1

    db = SessionLocal()
    try:
        row = (
            db.query(RecordingSession)
            .filter(RecordingSession.external_source == "omi", RecordingSession.external_id == "omi-sess-1")
            .first()
        )
        assert row is not None
        assert row.mode == "live"
        assert row.status == "active"
        segs = row.transcript_diarized["segments"]
        assert len(segs) == 2
        assert "Aaron" in row.transcript_diarized["speakers"]
        assert "Shafen" in row.transcript_diarized["speakers"]
        assert db.query(Transcription).filter(Transcription.session_id == row.id).count() == 2
    finally:
        db.close()


def test_realtime_transcript_is_idempotent_on_redelivery(client):
    """Omi may redeliver the tail of a session on reconnect; the same
    segment text/speaker/timestamps must not be double-counted."""
    token = _omi_token(client)
    payload = {
        "session_id": "omi-sess-redeliver",
        "segments": [{"text": "We decided to ship Friday.", "speaker": "Aaron", "start": 10.0, "end": 12.0}],
    }
    r1 = client.post(f"/api/v1/integrations/omi/{token}/realtime-transcript", json=payload)
    r2 = client.post(f"/api/v1/integrations/omi/{token}/realtime-transcript", json=payload)
    assert r1.json()["segments_new"] == 1
    assert r2.json()["segments_new"] == 0

    db = SessionLocal()
    try:
        row = (
            db.query(RecordingSession)
            .filter(RecordingSession.external_source == "omi", RecordingSession.external_id == "omi-sess-redeliver")
            .first()
        )
        assert len(row.transcript_diarized["segments"]) == 1
        assert db.query(Transcription).filter(Transcription.session_id == row.id).count() == 1
    finally:
        db.close()


def test_realtime_transcript_requires_session_id(client):
    token = _omi_token(client)
    resp = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={"session_id": "", "segments": []},
    )
    assert resp.status_code == 422


def test_realtime_transcript_two_orgs_do_not_collide(client):
    """Same Omi session_id string used by two different organizations must
    resolve to two independent sessions (tenant isolation)."""
    token_a = _omi_token(client, org_slug="magic-unicorn")

    # Create a second org + admin membership so we can mint a second PAT.
    db = SessionLocal()
    try:
        from auth.models import Organization, User, UserOrganization

        admin = db.query(User).filter(User.username == "admin").first()
        org_b = Organization(name="Org B", slug="org-b", is_active=True)
        db.add(org_b)
        db.flush()
        db.add(UserOrganization(user_id=admin.id, organization_id=org_b.id, role="admin"))
        db.commit()
    finally:
        db.close()

    token_b = _omi_token(client, org_slug="org-b")

    client.post(f"/api/v1/integrations/omi/{token_a}/realtime-transcript", json={"session_id": "shared-id", "segments": [{"text": "A's meeting", "speaker": "A"}]})
    client.post(f"/api/v1/integrations/omi/{token_b}/realtime-transcript", json={"session_id": "shared-id", "segments": [{"text": "B's meeting", "speaker": "B"}]})

    db = SessionLocal()
    try:
        rows = db.query(RecordingSession).filter(RecordingSession.external_source == "omi", RecordingSession.external_id == "shared-id").all()
        assert len(rows) == 2
        org_ids = {r.organization_id for r in rows}
        assert len(org_ids) == 2
        for r in rows:
            assert len(r.transcript_diarized["segments"]) == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Memory Creation: finalize pipeline
# ---------------------------------------------------------------------------

def test_memory_created_finalizes_session_and_runs_pipeline(client):
    token = _omi_token(client)

    # Patch the three finalize-pipeline calls so this test doesn't require a
    # live LLM/Qdrant — verifies ORCHESTRATION (that they are called with the
    # right session), not the providers themselves (covered separately).
    with (
        patch("api.uploads._summarize_session") as mock_summarize,
        patch("api.ai_insights._generate_ai_insights") as mock_insights,
        patch("services.semantic_search_service.semantic_search.index_session") as mock_index,
    ):
        async def _fake_summarize(db, session, **kwargs):
            session.summary = "Fake summary"

        mock_summarize.side_effect = _fake_summarize

        class _FakeInsights:
            def model_dump(self):
                return {"summary": "Fake summary", "action_items": [], "key_decisions": ["Ship Friday"], "follow_ups": []}

        async def _fake_gen_insights(*args, **kwargs):
            return _FakeInsights()

        mock_insights.side_effect = _fake_gen_insights

        resp = client.post(
            f"/api/v1/integrations/omi/{token}/memory-created",
            json={
                "id": "omi-memory-001",
                "created_at": "2026-09-01T10:00:00Z",
                "started_at": "2026-09-01T10:00:00Z",
                "finished_at": "2026-09-01T10:20:00Z",
                "transcript_segments": [
                    {"text": "We decided to ship on Friday.", "speaker": "Aaron", "start": 0.0, "end": 3.0},
                    {"text": "I will own the migration.", "speaker": "Shafen", "start": 3.0, "end": 6.0},
                ],
                "structured": {"omi_summary": "shipped decision"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is True
        assert body["segments_indexed"] == 2
        assert mock_summarize.called
        assert mock_index.called

    db = SessionLocal()
    try:
        row = db.query(RecordingSession).filter(RecordingSession.external_source == "omi", RecordingSession.external_id == "omi-memory-001").first()
        assert row is not None
        assert row.status == "completed"
        assert row.mode == "upload"
        assert row.summary == "Fake summary"
        assert row.extra_data["omi_structured"]["omi_summary"] == "shipped decision"
        assert db.query(Transcription).filter(Transcription.session_id == row.id).count() == 2
    finally:
        db.close()


def test_memory_created_is_idempotent_by_external_id(client):
    token = _omi_token(client)
    with (
        patch("api.uploads._summarize_session") as mock_summarize,
        patch("api.ai_insights._generate_ai_insights") as mock_insights,
        patch("services.semantic_search_service.semantic_search.index_session"),
    ):
        async def _noop(*a, **k):
            return None

        mock_summarize.side_effect = _noop

        class _FakeInsights:
            def model_dump(self):
                return {}

        async def _fake_gen_insights(*a, **k):
            return _FakeInsights()

        mock_insights.side_effect = _fake_gen_insights

        payload = {
            "id": "omi-memory-dup",
            "transcript_segments": [{"text": "Hello.", "speaker": "A", "start": 0.0, "end": 1.0}],
        }
        r1 = client.post(f"/api/v1/integrations/omi/{token}/memory-created", json=payload)
        r2 = client.post(f"/api/v1/integrations/omi/{token}/memory-created", json=payload)
        assert r1.json()["created"] is True
        assert r2.json()["created"] is False
        assert r1.json()["session_id"] == r2.json()["session_id"]

    db = SessionLocal()
    try:
        count = db.query(RecordingSession).filter(RecordingSession.external_source == "omi", RecordingSession.external_id == "omi-memory-dup").count()
        assert count == 1
    finally:
        db.close()


def test_memory_created_finalize_failure_does_not_lose_transcript(client):
    """A summarization/indexing failure must not discard the transcript that
    was already durably persisted — matches the upload pipeline's
    best-effort degrade-don't-discard behavior."""
    token = _omi_token(client)
    with patch("api.uploads._summarize_session", side_effect=RuntimeError("LLM down")):
        resp = client.post(
            f"/api/v1/integrations/omi/{token}/memory-created",
            json={"id": "omi-memory-degraded", "transcript_segments": [{"text": "Still recorded.", "speaker": "A", "start": 0.0, "end": 1.0}]},
        )
        assert resp.status_code == 200, resp.text

    db = SessionLocal()
    try:
        row = db.query(RecordingSession).filter(RecordingSession.external_source == "omi", RecordingSession.external_id == "omi-memory-degraded").first()
        assert row is not None
        assert "Still recorded." in (row.transcript or "")
    finally:
        db.close()
