"""Tests for services/providers/impl_lyzr.py.

These mock httpx at the transport boundary (legitimate per this codebase's
own mocking policy: mocks are for tests, never the production path). They
verify LyzrProvider builds the REAL documented Lyzr request shapes
(POST /v3/agents/, POST /v3/inference/chat/) correctly, handles
retry/permanent-error classification, and never fakes success when
required configuration is missing.
"""
from __future__ import annotations

import httpx
import pytest

from services.providers.impl_lyzr import LyzrNotConfigured, LyzrProvider
from services.providers.impl_llm import LLMUnavailable


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or str(json_data)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=httpx.Response(self.status_code))


@pytest.fixture(autouse=True)
def _lyzr_env(monkeypatch):
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "prov-123")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "cred-456")


@pytest.mark.asyncio
async def test_chat_creates_agent_once_then_reuses_cached_id(monkeypatch):
    calls = {"create_agent": 0, "chat": 0}

    async def fake_post(self, url, headers=None, json=None):
        if url.endswith("/v3/agents/"):
            calls["create_agent"] += 1
            assert json["llm_credential_id"] == "cred-456"
            assert json["provider_id"] == "prov-123"
            assert headers["x-api-key"] == "test-key"
            return _FakeResponse(200, {"id": "agent-abc"})
        if url.endswith("/v3/inference/chat/"):
            calls["chat"] += 1
            assert json["agent_id"] == "agent-abc"
            assert "message" in json
            return _FakeResponse(200, {"agent_response": "Ship on Friday.", "session_id": json["session_id"]})
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    provider = LyzrProvider(api_key="test-key", org_id=None, db=None)
    result1 = await provider.chat("system", "What did we decide?")
    result2 = await provider.chat("system", "Follow-up question")

    assert result1 == "Ship on Friday."
    assert result2 == "Ship on Friday."
    # Second call reuses the in-memory cached agent id — no second create call.
    assert calls["create_agent"] == 1
    assert calls["chat"] == 2


@pytest.mark.asyncio
async def test_chat_raises_not_configured_without_credentials(monkeypatch):
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "")

    provider = LyzrProvider(api_key="test-key", org_id=None, db=None)
    with pytest.raises(LyzrNotConfigured):
        await provider.chat("system", "hello")


@pytest.mark.asyncio
async def test_chat_raises_not_configured_without_api_key():
    provider = LyzrProvider(api_key="", org_id=None, db=None)
    with pytest.raises(LyzrNotConfigured):
        await provider.chat("system", "hello")


@pytest.mark.asyncio
async def test_chat_does_not_retry_auth_failure(monkeypatch):
    calls = {"chat": 0}

    async def fake_post(self, url, headers=None, json=None):
        if url.endswith("/v3/agents/"):
            return _FakeResponse(200, {"id": "agent-abc"})
        calls["chat"] += 1
        return _FakeResponse(401, {}, text="unauthorized")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = LyzrProvider(api_key="bad-key", org_id=None, db=None)
    with pytest.raises(LyzrNotConfigured):
        await provider.chat("system", "hello")
    assert calls["chat"] == 1  # not retried — permanent auth failure


@pytest.mark.asyncio
async def test_chat_does_not_retry_422_validation_error(monkeypatch):
    calls = {"chat": 0}

    async def fake_post(self, url, headers=None, json=None):
        if url.endswith("/v3/agents/"):
            return _FakeResponse(200, {"id": "agent-abc"})
        calls["chat"] += 1
        return _FakeResponse(422, {}, text="bad payload")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = LyzrProvider(api_key="k", org_id=None, db=None)
    with pytest.raises(LLMUnavailable):
        await provider.chat("system", "hello")
    assert calls["chat"] == 1


@pytest.mark.asyncio
async def test_chat_retries_transient_failures_then_succeeds(monkeypatch):
    calls = {"chat": 0}

    async def fake_post(self, url, headers=None, json=None):
        if url.endswith("/v3/agents/"):
            return _FakeResponse(200, {"id": "agent-abc"})
        calls["chat"] += 1
        if calls["chat"] < 3:
            raise httpx.ConnectTimeout("timed out")
        return _FakeResponse(200, {"agent_response": "recovered"})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("services.providers.impl_lyzr._BACKOFF_BASE_SECONDS", 0.01)
    provider = LyzrProvider(api_key="k", org_id=None, db=None)
    result = await provider.chat("system", "hello")
    assert result == "recovered"
    assert calls["chat"] == 3


@pytest.mark.asyncio
async def test_chat_gives_up_after_max_retries(monkeypatch):
    async def fake_post(self, url, headers=None, json=None):
        if url.endswith("/v3/agents/"):
            return _FakeResponse(200, {"id": "agent-abc"})
        raise httpx.ConnectTimeout("always down")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setattr("services.providers.impl_lyzr._BACKOFF_BASE_SECONDS", 0.01)
    provider = LyzrProvider(api_key="k", org_id=None, db=None)
    with pytest.raises(LLMUnavailable):
        await provider.chat("system", "hello")


@pytest.mark.asyncio
async def test_health_reports_unavailable_without_config(monkeypatch):
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "")
    provider = LyzrProvider(api_key="k", org_id=None, db=None)
    health = await provider.health()
    assert health["available"] is False
    assert "LYZR_PROVIDER_ID" in health["error"]


def test_registry_dispatches_lyzr_provider(monkeypatch):
    from services.providers.registry import ProviderRegistry

    class _FakeDB:
        def query(self, *a, **k):
            raise AssertionError("should not query DB for this simple test")

    registry = ProviderRegistry.__new__(ProviderRegistry)
    registry._db = None
    registry._settings_cache = {(1, "llm"): {"provider_name": "lyzr", "endpoint_url": "", "api_key_encrypted": "", "model_name": "", "overrides": {}}}

    def fake_get_api_key(self, org_id, kind):
        return "resolved-key"

    monkeypatch.setattr(ProviderRegistry, "get_api_key", fake_get_api_key)
    llm = registry.get_llm(org_id=1, task="quality")
    from services.providers.impl_lyzr import LyzrProvider as LP
    assert isinstance(llm, LP)
    assert llm.api_key == "resolved-key"
