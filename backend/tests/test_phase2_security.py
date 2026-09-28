"""Phase 2 security verification (task section 8).

Covers cases not already exercised by test_omi_webhooks.py /
test_lyzr_provider.py: revoked/expired PAT, wrong scope on the
memory-created endpoint specifically, malformed/oversized Omi payloads,
malformed Lyzr responses, an unauthenticated Ask AI request, and a check
that no secret material (PAT plaintext, Lyzr API key) leaks into error
responses.
"""
from __future__ import annotations

import httpx
import pytest

from database.database import SessionLocal


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_pat(client, scope: str, expires_in_days: int = 90, org_slug: str = "magic-unicorn") -> str:
    body = {"name": f"sec-test-{scope}", "scope": scope}
    if scope != "user":
        body["organization_slug"] = org_slug
        body["expires_in_days"] = expires_in_days
    created = client.post("/api/auth/pats", headers=_admin_bearer(client), json=body)
    assert created.status_code == 201, created.text
    return created.json()["plaintext"], created.json()["id"]


# ---------------------------------------------------------------------------
# PAT lifecycle: revoked / expired / missing / wrong scope
# ---------------------------------------------------------------------------

def test_revoked_omi_pat_is_rejected(client):
    token, pat_id = _create_pat(client, "omi.transcript.ingest")
    revoke = client.delete(f"/api/auth/pats/{pat_id}", headers=_admin_bearer(client))
    assert revoke.status_code in (200, 204), revoke.text

    resp = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={"session_id": "x", "segments": [{"text": "hi", "speaker": "A"}]},
    )
    assert resp.status_code == 401


def test_expired_omi_pat_is_rejected(client):
    token, pat_id = _create_pat(client, "omi.transcript.ingest", expires_in_days=1)
    # Force it into the past directly — there is no "expires_in_days: -1"
    # API surface (by design, so nobody can mint an already-expired token
    # through the API), so backdating via the DB is the only way to exercise
    # this path deterministically.
    db = SessionLocal()
    try:
        from datetime import datetime, timedelta, timezone

        from auth.models import PersonalAccessToken

        row = db.query(PersonalAccessToken).filter(PersonalAccessToken.id == pat_id).first()
        row.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        db.commit()
    finally:
        db.close()

    resp = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={"session_id": "x", "segments": [{"text": "hi", "speaker": "A"}]},
    )
    assert resp.status_code == 401


def test_missing_or_garbage_token_is_rejected(client):
    resp = client.post(
        "/api/v1/integrations/omi/not-a-real-token/realtime-transcript",
        json={"session_id": "x", "segments": []},
    )
    assert resp.status_code == 401
    # Also check the empty-path-segment shape resolves to 404 (route
    # doesn't exist) rather than silently authenticating as anonymous.
    resp2 = client.post(
        "/api/v1/integrations/omi//realtime-transcript",
        json={"session_id": "x", "segments": []},
    )
    assert resp2.status_code in (401, 404)


def test_wrong_scope_rejected_on_memory_created_endpoint_too(client):
    """test_omi_webhooks.py checks this for realtime-transcript; the
    memory-created endpoint shares the same auth dependency but is tested
    separately since it's a distinct route the scope check must also guard."""
    token, _ = _create_pat(client, "stable.transcript.ingest")
    resp = client.post(
        f"/api/v1/integrations/omi/{token}/memory-created",
        json={"id": "mem-1", "transcript_segments": []},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Malformed / oversized Omi payloads
# ---------------------------------------------------------------------------

def test_malformed_realtime_payload_missing_required_field(client):
    token, _ = _create_pat(client, "omi.transcript.ingest")
    # `segments` entries require `text`; send garbage shape entirely.
    resp = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={"segments": "this-should-be-a-list-not-a-string"},
    )
    assert resp.status_code == 422


def test_malformed_realtime_payload_wrong_types(client):
    token, _ = _create_pat(client, "omi.transcript.ingest")
    resp = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={"session_id": "x", "segments": [{"text": 12345, "speaker": True, "start": "not-a-number"}]},
    )
    assert resp.status_code == 422


def test_oversized_realtime_segments_payload_rejected(client):
    token, _ = _create_pat(client, "omi.transcript.ingest")
    # OmiRealtimeTranscriptPayload caps segments at 500 (Field max_length).
    oversized = {"session_id": "x", "segments": [{"text": "a", "speaker": "A"} for _ in range(501)]}
    resp = client.post(f"/api/v1/integrations/omi/{token}/realtime-transcript", json=oversized)
    assert resp.status_code == 422


def test_oversized_memory_transcript_payload_rejected(client):
    token, _ = _create_pat(client, "omi.transcript.ingest")
    # OmiMemoryPayload caps transcript_segments at 5000.
    oversized = {"id": "mem-huge", "transcript_segments": [{"text": "a", "speaker": "A"} for _ in range(5001)]}
    resp = client.post(f"/api/v1/integrations/omi/{token}/memory-created", json=oversized)
    assert resp.status_code == 422


def test_oversized_single_segment_text_rejected(client):
    token, _ = _create_pat(client, "omi.transcript.ingest")
    # Individual segment text is capped at 8000 chars.
    resp = client.post(
        f"/api/v1/integrations/omi/{token}/realtime-transcript",
        json={"session_id": "x", "segments": [{"text": "a" * 8001, "speaker": "A"}]},
    )
    assert resp.status_code == 422


def test_memory_created_missing_required_id_field(client):
    token, _ = _create_pat(client, "omi.transcript.ingest")
    resp = client.post(
        f"/api/v1/integrations/omi/{token}/memory-created",
        json={"transcript_segments": [{"text": "hi", "speaker": "A"}]},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Malformed Lyzr responses — must degrade gracefully, never crash the request
# ---------------------------------------------------------------------------

class _RawResponse:
    def __init__(self, status_code: int, raw_text: str):
        self.status_code = status_code
        self.text = raw_text

    def json(self):
        import json as _json
        return _json.loads(self.text)  # raises ValueError/JSONDecodeError on bad input

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=httpx.Response(self.status_code))


def test_lyzr_non_json_response_is_handled_without_crashing(monkeypatch):
    from services.providers.impl_lyzr import LyzrProvider, LLMUnavailable

    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "p")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "c")
    monkeypatch.setattr("services.providers.impl_lyzr._BACKOFF_BASE_SECONDS", 0.01)

    async def fake_post(self, url, headers=None, json=None):
        if url.endswith("/v3/agents/"):
            return _RawResponse(200, '{"id": "agent-1"}')
        return _RawResponse(200, "<html>not json, something upstream broke</html>")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    import asyncio

    provider = LyzrProvider(api_key="k", org_id=None, db=None)
    with pytest.raises(LLMUnavailable):
        asyncio.run(provider.chat("system", "hello"))


def test_lyzr_missing_agent_response_field_is_handled(monkeypatch):
    from services.providers.impl_lyzr import LyzrProvider, LLMUnavailable

    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "p")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "c")
    monkeypatch.setattr("services.providers.impl_lyzr._BACKOFF_BASE_SECONDS", 0.01)

    async def fake_post(self, url, headers=None, json=None):
        if url.endswith("/v3/agents/"):
            return _RawResponse(200, '{"id": "agent-1"}')
        # Real Lyzr responses key on "agent_response"; simulate a schema
        # drift where that key is simply absent.
        return _RawResponse(200, '{"status": "success", "unexpected_key": "value"}')

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    import asyncio

    provider = LyzrProvider(api_key="k", org_id=None, db=None)
    with pytest.raises(LLMUnavailable):
        asyncio.run(provider.chat("system", "hello"))


def test_lyzr_chat_sync_malformed_response_returns_empty_not_crash(monkeypatch):
    """chat_sync's contract (matching every other provider) is to return ''
    on failure rather than raise, since call sites treat '' as 'no answer'
    and degrade — it must never bubble a raw exception into a request
    handler."""
    from services.providers.impl_lyzr import LyzrProvider

    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "p")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "c")
    monkeypatch.setattr("services.providers.impl_lyzr._BACKOFF_BASE_SECONDS", 0.01)

    def fake_post_sync(self, url, headers=None, json=None):
        if url.endswith("/v3/agents/"):
            return _RawResponse(200, '{"id": "agent-1"}')
        return _RawResponse(200, "not json at all")

    monkeypatch.setattr(httpx.Client, "post", fake_post_sync)

    provider = LyzrProvider(api_key="k", org_id=None, db=None)
    result = provider.chat_sync("system", "hello")
    assert result == ""


# ---------------------------------------------------------------------------
# Unauthorized Ask AI request
# ---------------------------------------------------------------------------

def test_ask_ai_requires_authentication(client):
    resp = client.post("/api/ai-chat/rag/query", json={"message": "What did we decide?", "limit": 5})
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Secrets never leak into error responses
# ---------------------------------------------------------------------------

def test_pat_plaintext_never_appears_in_list_response(client):
    token, _ = _create_pat(client, "omi.transcript.ingest")
    listing = client.get("/api/auth/pats", headers=_admin_bearer(client))
    assert listing.status_code == 200
    assert token not in listing.text


def test_lyzr_error_response_does_not_leak_api_key(monkeypatch):
    """A Lyzr HTTP failure surfaced up through LyzrProvider must not embed
    the raw api_key anywhere in its exception message (it's built from
    Lyzr's response body/status, never from self.api_key)."""
    from services.providers.impl_lyzr import LyzrProvider, LyzrNotConfigured

    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "p")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "c")

    secret_key = "sk-super-secret-do-not-leak-123456"

    async def fake_post(self, url, headers=None, json=None):
        assert headers["x-api-key"] == secret_key  # confirm it WAS sent...
        return _RawResponse(401, '{"detail": "unauthorized"}')

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    import asyncio

    provider = LyzrProvider(api_key=secret_key, org_id=None, db=None)
    with pytest.raises(LyzrNotConfigured) as excinfo:
        asyncio.run(provider.chat("system", "hello"))
    # ...but never echoed back in the exception seen by logs/callers.
    assert secret_key not in str(excinfo.value)
