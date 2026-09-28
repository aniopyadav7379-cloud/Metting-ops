"""Unit tests for the outbound Project-Ops federation token.

Covers ``services.projectops_token.projectops_federation_token`` — the
Brigade RFC-8693 exchange that mints a workspace-bound, ``aud=project-ops``
token for the MO -> Project-Ops triage push. Mirrors the Contact-Ops resolver
tests: dormant-safety (no network when unconfigured), the correct exchange
form, and graceful ``None`` on every failure. Token plumbing + httpx are
monkeypatched — no real network.
"""

from __future__ import annotations

import pytest

from services import projectops_token as t


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    t._token_cache.clear()
    # neutral env baseline
    for k in (
        "MEETING_OPS_KC_CLIENT_SECRET",
        "MEETING_OPS_KC_CLIENT_ID",
        "MEETING_OPS_KC_TOKEN_URL",
        "BRIGADE_EXCHANGE_URL",
        "PROJECTOPS_FEDERATION_AUDIENCE",
        "PROJECTOPS_FEDERATION_SCOPE",
    ):
        monkeypatch.delenv(k, raising=False)
    yield
    t._token_cache.clear()


def _enable(monkeypatch):
    monkeypatch.setenv("MEETING_OPS_KC_CLIENT_SECRET", "secret-xyz")


# ── dormant safety ─────────────────────────────────────────────────────


def test_disabled_by_default():
    assert t.is_configured() is False


def test_enabled_needs_secret(monkeypatch):
    assert t.is_configured() is False  # no secret
    monkeypatch.setenv("MEETING_OPS_KC_CLIENT_SECRET", "s")
    assert t.is_configured() is True


@pytest.mark.asyncio
async def test_dormant_no_network(monkeypatch):
    # No secret -> returns None and never builds a token.
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        return "x"

    monkeypatch.setattr(t, "_project_ops_token", _boom)
    assert await t.projectops_federation_token("ws-1") is None
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_blank_workspace_returns_none(monkeypatch):
    _enable(monkeypatch)
    assert await t.projectops_federation_token("") is None
    assert await t.projectops_federation_token("   ") is None


# ── exchange form + parsing (httpx mocked) ─────────────────────────────


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Records every POST (url + form data) so the test can assert the
    exchange form, and returns programmed responses in call order."""

    def __init__(self, responses):
        # responses: list of (status, payload), consumed per POST.
        self._responses = list(responses)
        self.posts: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self.posts.append({"url": url, "data": kw.get("data")})
        status, payload = self._responses.pop(0)
        return _FakeResp(status, payload)


def _wire(monkeypatch, responses):
    _enable(monkeypatch)
    fake = _FakeClient(responses)
    monkeypatch.setattr(t.httpx, "AsyncClient", lambda *a, **k: fake)
    return fake


@pytest.mark.asyncio
async def test_exchange_posts_correct_form(monkeypatch):
    """The happy path: a mint then an exchange. Assert BOTH the
    client-credentials mint and the RFC-8693 exchange carry the right form,
    and the exchanged access_token is returned."""
    fake = _wire(
        monkeypatch,
        responses=[
            (200, {"access_token": "subject-tok", "expires_in": 300}),
            (200, {"access_token": "po-fed-tok", "expires_in": 300}),
        ],
    )

    tok = await t.projectops_federation_token("ws-abc")
    assert tok == "po-fed-tok"

    # 1) the client-credentials mint.
    mint = fake.posts[0]
    assert mint["url"] == t._kc_token_url()
    assert mint["data"]["grant_type"] == "client_credentials"
    assert mint["data"]["client_id"] == "meeting-ops"
    assert mint["data"]["client_secret"] == "secret-xyz"

    # 2) the Brigade token-exchange — the load-bearing assertion.
    ex = fake.posts[1]
    assert ex["url"] == t._brigade_exchange_url()
    assert ex["data"]["grant_type"] == (
        "urn:ietf:params:oauth:grant-type:token-exchange"
    )
    assert ex["data"]["subject_token"] == "subject-tok"
    assert ex["data"]["audience"] == "project-ops"
    assert ex["data"]["workspace_id"] == "ws-abc"
    assert ex["data"]["scope"] == "triage:write"


@pytest.mark.asyncio
async def test_audience_overridable_via_env(monkeypatch):
    monkeypatch.setenv("PROJECTOPS_FEDERATION_AUDIENCE", "project-ops-staging")
    fake = _wire(
        monkeypatch,
        responses=[
            (200, {"access_token": "subject-tok", "expires_in": 300}),
            (200, {"access_token": "po-fed-tok", "expires_in": 300}),
        ],
    )
    tok = await t.projectops_federation_token("ws-abc")
    assert tok == "po-fed-tok"
    assert fake.posts[1]["data"]["audience"] == "project-ops-staging"


@pytest.mark.asyncio
async def test_token_alias_field(monkeypatch):
    # Brigade may return the exchanged token under ``token`` instead of
    # ``access_token`` (resolver parity).
    _wire(
        monkeypatch,
        responses=[
            (200, {"access_token": "subject-tok", "expires_in": 300}),
            (200, {"token": "po-fed-tok-alias", "expires_in": 300}),
        ],
    )
    assert await t.projectops_federation_token("ws-abc") == "po-fed-tok-alias"


@pytest.mark.asyncio
async def test_mint_failure_returns_none(monkeypatch):
    # KC mint 401 -> None, and no exchange is attempted.
    fake = _wire(monkeypatch, responses=[(401, {})])
    assert await t.projectops_federation_token("ws-abc") is None
    assert len(fake.posts) == 1  # mint only, no exchange


@pytest.mark.asyncio
async def test_exchange_failure_returns_none(monkeypatch):
    # Brigade exchange 403 (e.g. actor not yet allowed for aud=project-ops)
    # -> None; the caller fails closed without a Project-Ops request.
    _wire(
        monkeypatch,
        responses=[
            (200, {"access_token": "subject-tok", "expires_in": 300}),
            (403, {}),
        ],
    )
    assert await t.projectops_federation_token("ws-abc") is None


@pytest.mark.asyncio
async def test_exchange_missing_token_returns_none(monkeypatch):
    _wire(
        monkeypatch,
        responses=[
            (200, {"access_token": "subject-tok", "expires_in": 300}),
            (200, {"expires_in": 300}),  # no token field
        ],
    )
    assert await t.projectops_federation_token("ws-abc") is None


@pytest.mark.asyncio
async def test_per_workspace_cache(monkeypatch):
    # Second call for the same workspace is served from cache (no new POSTs).
    fake = _wire(
        monkeypatch,
        responses=[
            (200, {"access_token": "subject-tok", "expires_in": 300}),
            (200, {"access_token": "po-fed-tok", "expires_in": 300}),
        ],
    )
    first = await t.projectops_federation_token("ws-cache")
    assert first == "po-fed-tok"
    posts_after_first = len(fake.posts)

    second = await t.projectops_federation_token("ws-cache")
    assert second == "po-fed-tok"
    assert len(fake.posts) == posts_after_first  # cached, no extra network
