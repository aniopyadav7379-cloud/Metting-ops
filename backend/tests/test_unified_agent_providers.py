"""Unified-agent /providers honesty (audit finding: hardcoded fake catalog).

GET /api/unified-agent/providers used to return a hardcoded provider list —
Ollama "available: True" without checking, OpenAI/Anthropic hardcoded False,
and a stale "granite3.3:8b" label. It now derives from the same env
resolution ProviderRegistry.get_llm uses (MEETING_OPS_LLM_* direct route,
else the LiteLLM gateway + LLM_MODEL_{FAST,QUALITY,CHAT}) and makes no
availability claims. POST /api/unified-agent/test (canned John/Sarah/Mike
transcript piped to raw Ollama) was removed outright.
"""
from __future__ import annotations


def _admin_headers(client):
    resp = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": "admin123"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _clear_llm_env(monkeypatch):
    for key in (
        "MEETING_OPS_LLM_URL",
        "MEETING_OPS_LLM_MODEL",
        "MEETING_OPS_SUMMARIZER_URL",
        "MEETING_OPS_SUMMARIZER_MODEL",
        "LLM_MODEL_FAST",
        "LLM_MODEL_QUALITY",
        "LLM_MODEL_CHAT",
        "OPENAI_API_BASE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_providers_requires_auth(client):
    resp = client.get("/api/unified-agent/providers")
    assert resp.status_code == 401, resp.text


def test_providers_reports_gateway_route_from_env(client, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("LLM_MODEL_QUALITY", "Qwen3.6-35B-A3B-Vision")
    monkeypatch.setenv("LLM_MODEL_FAST", "gemma-4-e4b")
    monkeypatch.setenv("OPENAI_API_BASE", "http://unicorn-litellm:4000/v1")

    resp = client.get("/api/unified-agent/providers", headers=_admin_headers(client))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["default"] == "litellm"
    assert len(data["providers"]) == 1
    gateway = data["providers"][0]
    assert gateway["type"] == "litellm"
    assert gateway["active"] is True
    assert gateway["endpoint"] == "http://unicorn-litellm:4000/v1"
    assert gateway["models"]["quality"] == "Qwen3.6-35B-A3B-Vision"
    assert gateway["models"]["fast"] == "gemma-4-e4b"
    # No env for chat -> registry code default, not a legacy label.
    assert gateway["models"]["chat"] == "Qwen3.6-35B-A3B-Vision"

    # No fabricated availability claims and no stale legacy stack labels.
    body = resp.text.lower()
    for provider in data["providers"]:
        assert "available" not in provider
    assert "granite" not in body
    assert "ollama" not in body
    assert "anthropic" not in body


def test_providers_reports_direct_route_when_configured(client, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("MEETING_OPS_LLM_URL", "http://midboy1:8000/v1")
    monkeypatch.setenv("MEETING_OPS_LLM_MODEL", "Qwen3.6-35B-A3B-Vision")

    resp = client.get("/api/unified-agent/providers", headers=_admin_headers(client))
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["default"] == "direct"
    direct = data["providers"][0]
    assert direct["type"] == "direct"
    assert direct["active"] is True
    assert direct["endpoint"] == "http://midboy1:8000/v1"
    assert direct["models"] == {
        "fast": "Qwen3.6-35B-A3B-Vision",
        "quality": "Qwen3.6-35B-A3B-Vision",
        "chat": "Qwen3.6-35B-A3B-Vision",
    }
    # Gateway route still listed, but explicitly not the active default.
    gateway = data["providers"][1]
    assert gateway["type"] == "litellm"
    assert gateway["active"] is False


def test_providers_honors_legacy_summarizer_envs(client, monkeypatch):
    _clear_llm_env(monkeypatch)
    monkeypatch.setenv("MEETING_OPS_SUMMARIZER_URL", "http://midboy1:8000/v1")
    monkeypatch.setenv("MEETING_OPS_SUMMARIZER_MODEL", "Qwen3.6-35B-A3B-Vision")

    resp = client.get("/api/unified-agent/providers", headers=_admin_headers(client))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["default"] == "direct"
    assert data["providers"][0]["endpoint"] == "http://midboy1:8000/v1"


def test_canned_test_endpoint_removed(client):
    resp = client.post(
        "/api/unified-agent/test", headers=_admin_headers(client)
    )
    assert resp.status_code == 404, resp.text
