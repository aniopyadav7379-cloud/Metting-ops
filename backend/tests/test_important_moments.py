"""Tests for api/important_moments.py and services/important_moments.py.

The core thing under test is timestamp GROUNDING: a moment's timestamp
must come from a real Transcription row whose text actually contains the
LLM's quote, never from a number the LLM claims directly — mirroring the
'never invent a source' rule applied specifically to timestamps.
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_session_with_segments(segments: list[tuple[str, str, float, float]]) -> str:
    """segments: list of (speaker, text, start, end)."""
    from database.database import SessionLocal
    from database.models import RecordingSession, Transcription

    db = SessionLocal()
    try:
        s = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="Architecture Review",
            title="Architecture Review",
            organization_id=1,
            status="completed",
            transcript_simple=" ".join(f"{sp}: {t}" for sp, t, _, _ in segments),
        )
        db.add(s)
        db.flush()
        for speaker, text, start, end in segments:
            db.add(Transcription(session_id=s.id, text=text, speaker=speaker, start_time=start, end_time=end))
        db.commit()
        db.refresh(s)
        return s.session_id
    finally:
        db.close()


SEGMENTS = [
    ("Aaron", "Let's finalize the plan for the migration.", 0.0, 3.0),
    ("Shafen", "We decided to ship the migration next Friday.", 3.0, 6.0),
    ("Aaron", "Great, I'll own the rollback plan in case it fails.", 6.0, 9.0),
]


def _configure_lyzr(monkeypatch, response_json: dict):
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "prov-mom")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "cred-mom")

    from database.database import SessionLocal
    from database.models import OrgProviderSettings
    from services.providers.crypto import encrypt_api_key

    db = SessionLocal()
    db.query(OrgProviderSettings).filter(OrgProviderSettings.organization_id == 1, OrgProviderSettings.service_kind == "llm").delete()
    db.commit()
    db.add(
        OrgProviderSettings(
            organization_id=1, service_kind="llm", provider_name="lyzr",
            endpoint_url="https://agent-prod.studio.lyzr.ai",
            api_key_encrypted=encrypt_api_key("k"), model_name="", overrides={},
        )
    )
    db.commit()
    db.close()

    original_client_post = httpx.Client.post

    def fake_post_sync(self, url, *args, headers=None, json=None, **kwargs):
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-mom-1"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            return _FakeResponse(200, {"agent_response": jd(response_json)})
        return original_client_post(self, url, *args, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", fake_post_sync)


def jd(x):
    return json.dumps(x)


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
        self.text = str(json_data)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=httpx.Response(self.status_code))


def test_moment_with_matching_quote_gets_real_timestamp(client, monkeypatch):
    _configure_lyzr(
        monkeypatch,
        {
            "moments": [
                {
                    "type": "decision",
                    "description": "Team decided to ship the migration next Friday",
                    "quote": "We decided to ship the migration next Friday",
                }
            ]
        },
    )
    session_id = _create_session_with_segments(SEGMENTS)
    headers = _admin_bearer(client)
    resp = client.post(f"/api/moments/{session_id}/extract", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["moment_type"] == "decision"
    assert body[0]["timestamp"] == 3.0
    assert body[0]["speaker"] == "Shafen"


def test_moment_with_unmatched_quote_gets_no_fabricated_timestamp(client, monkeypatch):
    """The LLM claims a quote that never appears anywhere in the real
    transcript — the moment is still kept (the description may be valid)
    but timestamp/speaker must be None, not guessed."""
    _configure_lyzr(
        monkeypatch,
        {
            "moments": [
                {
                    "type": "conclusion",
                    "description": "Meeting wrapped up with next steps agreed",
                    "quote": "this exact sentence was never said by anyone in the meeting",
                }
            ]
        },
    )
    session_id = _create_session_with_segments(SEGMENTS)
    headers = _admin_bearer(client)
    resp = client.post(f"/api/moments/{session_id}/extract", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["timestamp"] is None
    assert body[0]["speaker"] is None


def test_invalid_moment_type_from_llm_is_dropped(client, monkeypatch):
    _configure_lyzr(
        monkeypatch,
        {
            "moments": [
                {"type": "not_a_real_type", "description": "should be dropped", "quote": "Let's finalize the plan"},
                {"type": "decision", "description": "valid one", "quote": "We decided to ship the migration next Friday"},
            ]
        },
    )
    session_id = _create_session_with_segments(SEGMENTS)
    headers = _admin_bearer(client)
    resp = client.post(f"/api/moments/{session_id}/extract", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["moment_type"] == "decision"


def test_extract_requires_transcript(client):
    session_id = _create_session_with_segments([])
    headers = _admin_bearer(client)
    resp = client.post(f"/api/moments/{session_id}/extract", headers=headers)
    assert resp.status_code == 400


def test_re_extraction_replaces_previous_moments(client, monkeypatch):
    _configure_lyzr(monkeypatch, {"moments": [{"type": "decision", "description": "first pass", "quote": "Let's finalize the plan"}]})
    session_id = _create_session_with_segments(SEGMENTS)
    headers = _admin_bearer(client)
    first = client.post(f"/api/moments/{session_id}/extract", headers=headers)
    assert len(first.json()) == 1

    _configure_lyzr(monkeypatch, {"moments": [
        {"type": "decision", "description": "second pass A", "quote": "We decided to ship the migration next Friday"},
        {"type": "action_item", "description": "second pass B", "quote": "I'll own the rollback plan"},
    ]})
    second = client.post(f"/api/moments/{session_id}/extract", headers=headers)
    assert len(second.json()) == 2

    listing = client.get(f"/api/moments/{session_id}", headers=headers)
    assert len(listing.json()) == 2  # first pass's moment was replaced, not accumulated


def test_org_wide_timeline_filters_by_type(client, monkeypatch):
    _configure_lyzr(monkeypatch, {"moments": [
        {"type": "decision", "description": "d1", "quote": "We decided to ship the migration next Friday"},
        {"type": "action_item", "description": "a1", "quote": "I'll own the rollback plan"},
    ]})
    session_id = _create_session_with_segments(SEGMENTS)
    headers = _admin_bearer(client)
    client.post(f"/api/moments/{session_id}/extract", headers=headers)

    resp = client.get("/api/moments?moment_type=decision", headers=headers)
    assert resp.status_code == 200
    types = {m["moment_type"] for m in resp.json()}
    assert types == {"decision"}


def test_moments_endpoint_requires_auth(client):
    resp = client.get("/api/moments")
    assert resp.status_code in (401, 403)
