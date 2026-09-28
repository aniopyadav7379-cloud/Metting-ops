"""Unit tests for the outbound Contact-Ops resolver.

Covers dormant-safety (no network when disabled/unconfigured) and the
find_person_by_identifier response parsing (single match / empty / ambiguous /
error), with the token plumbing + MCP call monkeypatched — no real network.
"""

from __future__ import annotations

import pytest

from services import contact_ops_resolver as r


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    r._token_cache.clear()
    # neutral env baseline
    for k in (
        "CONTACT_OPS_RESOLVER_ENABLED",
        "MEETING_OPS_KC_CLIENT_SECRET",
    ):
        monkeypatch.delenv(k, raising=False)
    yield
    r._token_cache.clear()


def _enable(monkeypatch):
    monkeypatch.setenv("CONTACT_OPS_RESOLVER_ENABLED", "true")
    monkeypatch.setenv("MEETING_OPS_KC_CLIENT_SECRET", "secret-xyz")


# ── dormant safety ─────────────────────────────────────────────────────


def test_disabled_by_default():
    assert r.is_configured() is False


def test_enabled_needs_secret(monkeypatch):
    monkeypatch.setenv("CONTACT_OPS_RESOLVER_ENABLED", "true")
    assert r.is_configured() is False  # no secret
    monkeypatch.setenv("MEETING_OPS_KC_CLIENT_SECRET", "s")
    assert r.is_configured() is True


@pytest.mark.asyncio
async def test_resolve_dormant_no_network(monkeypatch):
    # No env -> returns None and never builds a token.
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return "x"

    monkeypatch.setattr(r, "_contact_ops_token", _boom)
    assert await r.resolve_email("a@b.com", "ws-1") is None
    assert called["n"] == 0


# ── parsing (token + httpx mocked) ─────────────────────────────────────


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        return _FakeResp(self._status, self._payload)


def _wire(monkeypatch, payload, status=200):
    _enable(monkeypatch)
    monkeypatch.setattr(r.httpx, "AsyncClient", lambda *a, **k: _FakeClient(payload, status))

    async def _tok(_client, _ws):
        return "co-token"

    monkeypatch.setattr(r, "_contact_ops_token", _tok)


def _envelope(structured):
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"structuredContent": structured, "isError": False}}


@pytest.mark.asyncio
async def test_single_match_returns_person_id(monkeypatch):
    _wire(monkeypatch, _envelope({"matches": [{"person_id": "p-123", "display_name": "X"}],
                                  "ambiguous": False}))
    assert await r.resolve_email("a@b.com", "ws-1") == "p-123"


@pytest.mark.asyncio
async def test_empty_match_returns_none(monkeypatch):
    _wire(monkeypatch, _envelope({"matches": [], "ambiguous": False}))
    assert await r.resolve_email("a@b.com", "ws-1") is None


@pytest.mark.asyncio
async def test_ambiguous_returns_none(monkeypatch):
    _wire(monkeypatch, _envelope({"matches": [{"person_id": "p1"}, {"person_id": "p2"}],
                                  "ambiguous": True}))
    assert await r.resolve_email("a@b.com", "ws-1") is None


@pytest.mark.asyncio
async def test_text_fallback_parse(monkeypatch):
    import json
    payload = {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text",
                     "text": json.dumps({"matches": [{"person_id": "p-text"}], "ambiguous": False})}],
        "isError": False}}
    _wire(monkeypatch, payload)
    assert await r.resolve_email("a@b.com", "ws-1") == "p-text"


@pytest.mark.asyncio
async def test_tool_error_returns_none(monkeypatch):
    _wire(monkeypatch, {"jsonrpc": "2.0", "id": 1, "result": {"isError": True,
          "content": [{"type": "text", "text": "boom"}]}})
    assert await r.resolve_email("a@b.com", "ws-1") is None


@pytest.mark.asyncio
async def test_http_500_returns_none(monkeypatch):
    _wire(monkeypatch, {}, status=500)
    assert await r.resolve_email("a@b.com", "ws-1") is None


@pytest.mark.asyncio
async def test_blank_email_returns_none(monkeypatch):
    _enable(monkeypatch)
    assert await r.resolve_email("", "ws-1") is None
    assert await r.resolve_email("a@b.com", "") is None


@pytest.mark.asyncio
async def test_search_people_returns_summaries(monkeypatch):
    _wire(monkeypatch, _envelope({
        "people": [
            {"person_id": "p-1", "display_name": "Ada Lovelace", "email": "ada@example.com"},
            {"id": "p-2", "name": "Grace Hopper", "emails": [{"value": "grace@example.com"}]},
            {"id": "p-1", "display_name": "Duplicate"},
        ]
    }))
    assert await r.search_people("ada", "ws-1") == [
        {
            "person_id": "p-1",
            "display_name": "Ada Lovelace",
            "email": "ada@example.com",
            "match_confidence": 0.0,
            "match_basis": "unknown",
        },
        {
            "person_id": "p-2",
            "display_name": "Grace Hopper",
            "email": "grace@example.com",
            "match_confidence": 0.0,
            "match_basis": "unknown",
        },
    ]


@pytest.mark.asyncio
async def test_search_people_dormant_no_network(monkeypatch):
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(r, "_call_contact_ops_tool", _boom)
    assert await r.search_people("ada", "ws-1") == []
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_search_people_preserves_explicit_confidence(monkeypatch):
    _wire(monkeypatch, _envelope({
        "items": [
            {
                "person_id": "p-1",
                "display_name": "Ada",
                "primary_email": "ada@example.com",
                "match_confidence": 1.0,
                "match_basis": "exact_email",
            }
        ],
        "ambiguous": False,
    }))
    assert await r.search_people("ada@example.com", "ws-1") == [
        {
            "person_id": "p-1",
            "display_name": "Ada",
            "email": "ada@example.com",
            "match_confidence": 1.0,
            "match_basis": "exact_email",
        }
    ]


@pytest.mark.asyncio
async def test_resolve_person_id_contract(monkeypatch):
    _wire(monkeypatch, _envelope({
        "requested_person_id": "stale",
        "canonical_person_id": "canonical",
        "alias_ids": ["stale", "older"],
        "status": "redirected",
    }))
    assert await r.resolve_person_id("stale", "ws-1") == {
        "requested_person_id": "stale",
        "canonical_person_id": "canonical",
        "alias_ids": ["stale", "older"],
        "status": "redirected",
    }
