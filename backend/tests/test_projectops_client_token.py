"""Tests for the ProjectOpsClient auth-token modes.

Covers the contract for ``services.projectops_client._TokenSource`` +
``ProjectOpsClient.is_live / auth_mode``:

   1. unconfigured_is_not_live           — no env, no kwargs => is_live False,
                                            mode='unconfigured'.
   2. static_token_is_live               — PROJECTOPS_API_KEY (env or kwarg)
                                            => is_live True, mode='static',
                                            requests bear the literal token.
   3. auto_refresh_wins_when_both_set    — KC creds AND static token both
                                            set => auto-refresh mode, static
                                            token ignored.
   4. mint_caches_until_exp              — first call mints; second call uses
                                            cache; cache horizon respects the
                                            JWT's exp claim minus safety
                                            buffer.
   5. mint_remints_after_expiry          — clock past cache_until => next
                                            call mints fresh.
   6. concurrent_mints_serialize         — N concurrent get() calls past
                                            expiry trigger exactly ONE
                                            network mint (asyncio.Lock).
   7. mint_failure_raises                — KC 401 => ProjectOpsClientError;
                                            no token cached, next attempt
                                            tries again.

The KC token endpoint is faked via an injected callable on
``_TokenSource._http`` (an httpx.MockTransport-backed AsyncClient).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx
import pytest


def _make_fake_jwt(*, exp_epoch: float) -> str:
    """Mint a JWT-shaped string with a real ``exp`` claim and a fake
    signature. We never verify the sig, we only decode the payload."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload_raw = json.dumps({"exp": int(exp_epoch), "sub": "test"}).encode()
    payload = base64.urlsafe_b64encode(payload_raw).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


class _MintTracker:
    """Counts mint requests + lets the test program the responses."""

    def __init__(self) -> None:
        self.calls = 0
        # Default: a 5-minute token, success.
        self._next_token = _make_fake_jwt(exp_epoch=time.time() + 300)
        self._next_status = 200

    def set_next(self, *, token: str = "", status: int = 200) -> None:
        if token:
            self._next_token = token
        self._next_status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self._next_status != 200:
            return httpx.Response(self._next_status, text="kc rejected")
        return httpx.Response(
            200,
            json={"access_token": self._next_token, "expires_in": 300},
        )


def _attach_tracker(token_source, tracker: _MintTracker) -> None:
    """Swap the token-source's httpx client for one backed by the tracker."""
    transport = httpx.MockTransport(tracker)
    token_source._http = httpx.AsyncClient(transport=transport, timeout=5.0)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in [
        "PROJECTOPS_API_KEY",
        "PROJECTOPS_BASE_URL",
        "PROJECTOPS_API_BASE_URL",
        "PROJECTOPS_KC_TOKEN_URL",
        "PROJECTOPS_KC_CLIENT_ID",
        "PROJECTOPS_KC_CLIENT_SECRET",
    ]:
        monkeypatch.delenv(k, raising=False)


def test_unconfigured_is_not_live():
    from services.projectops_client import ProjectOpsClient

    client = ProjectOpsClient()
    assert client.is_live is False
    assert client.auth_mode == "unconfigured"


def test_deploy_api_base_url_alias_is_honored(monkeypatch):
    from services.projectops_client import ProjectOpsClient

    monkeypatch.setenv(
        "PROJECTOPS_API_BASE_URL",
        "https://project-ops.example.test/api/v1/",
    )
    client = ProjectOpsClient()
    assert client._base_url == "https://project-ops.example.test/api/v1"


def test_canonical_base_url_wins_over_deploy_alias(monkeypatch):
    from services.projectops_client import ProjectOpsClient

    monkeypatch.setenv("PROJECTOPS_BASE_URL", "https://canonical.example.test/v1")
    monkeypatch.setenv("PROJECTOPS_API_BASE_URL", "https://legacy.example.test/v1")
    client = ProjectOpsClient()
    assert client._base_url == "https://canonical.example.test/v1"


def test_static_token_is_live():
    from services.projectops_client import ProjectOpsClient

    client = ProjectOpsClient(api_key="static-xyz")
    assert client.is_live is True
    assert client.auth_mode == "static"


def test_auto_refresh_wins_when_both_set():
    from services.projectops_client import ProjectOpsClient

    client = ProjectOpsClient(
        api_key="static-loses",
        token_url="https://auth.test/realms/uchub/protocol/openid-connect/token",
        kc_client_id="project-ops",
        kc_client_secret="topsecret",
    )
    assert client.is_live is True
    assert client.auth_mode == "auto-refresh"


@pytest.mark.asyncio
async def test_static_token_returned_verbatim():
    from services.projectops_client import ProjectOpsClient

    client = ProjectOpsClient(api_key="raw-bearer-abc")
    tok = await client._token_source.get()
    assert tok == "raw-bearer-abc"


@pytest.mark.asyncio
async def test_mint_caches_until_exp():
    from services.projectops_client import ProjectOpsClient

    # Five-minute token; safety buffer is 30s so cache horizon is ~270s.
    fixed_exp = time.time() + 300
    tracker = _MintTracker()
    tracker.set_next(token=_make_fake_jwt(exp_epoch=fixed_exp))

    client = ProjectOpsClient(
        token_url="https://auth.test/token",
        kc_client_id="project-ops",
        kc_client_secret="s3cr3t",
    )
    _attach_tracker(client._token_source, tracker)

    t1 = await client._token_source.get()
    t2 = await client._token_source.get()
    t3 = await client._token_source.get()
    assert t1 == t2 == t3
    assert tracker.calls == 1  # cached
    await client.aclose()


@pytest.mark.asyncio
async def test_mint_remints_after_expiry():
    from services.projectops_client import ProjectOpsClient

    tracker = _MintTracker()
    initial = _make_fake_jwt(exp_epoch=time.time() + 600)
    tracker.set_next(token=initial)

    client = ProjectOpsClient(
        token_url="https://auth.test/token",
        kc_client_id="project-ops",
        kc_client_secret="s3cr3t",
    )
    _attach_tracker(client._token_source, tracker)

    first = await client._token_source.get()
    assert first == initial
    assert tracker.calls == 1

    # Simulate cache expiry without sleeping: force the cached horizon into
    # the past. This is the same code path a real exp-driven eviction takes.
    client._token_source._cached_until = time.time() - 1.0
    fresh = _make_fake_jwt(exp_epoch=time.time() + 600)
    tracker.set_next(token=fresh)

    second = await client._token_source.get()
    assert second == fresh
    assert tracker.calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_mints_serialize():
    from services.projectops_client import ProjectOpsClient

    tracker = _MintTracker()
    tracker.set_next(token=_make_fake_jwt(exp_epoch=time.time() + 600))

    client = ProjectOpsClient(
        token_url="https://auth.test/token",
        kc_client_id="project-ops",
        kc_client_secret="s3cr3t",
    )
    _attach_tracker(client._token_source, tracker)

    # 20 concurrent gets, all racing the same cold cache.
    tokens = await asyncio.gather(
        *(client._token_source.get() for _ in range(20))
    )
    assert all(t == tokens[0] for t in tokens)
    assert tracker.calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_mint_failure_raises_and_does_not_cache():
    from services.projectops_client import (
        ProjectOpsClient,
        ProjectOpsClientError,
    )

    tracker = _MintTracker()
    tracker.set_next(status=401)

    client = ProjectOpsClient(
        token_url="https://auth.test/token",
        kc_client_id="project-ops",
        kc_client_secret="wrong",
    )
    _attach_tracker(client._token_source, tracker)

    with pytest.raises(ProjectOpsClientError):
        await client._token_source.get()
    # Second call also fails (no token was cached).
    tracker.set_next(status=401)
    with pytest.raises(ProjectOpsClientError):
        await client._token_source.get()
    assert tracker.calls == 2
    await client.aclose()


def test_env_resolution_paths(monkeypatch):
    """Env-based wiring matches the kwarg path."""
    from services.projectops_client import ProjectOpsClient

    monkeypatch.setenv("PROJECTOPS_API_KEY", "static-env")
    c1 = ProjectOpsClient()
    assert c1.auth_mode == "static"

    monkeypatch.setenv("PROJECTOPS_KC_TOKEN_URL", "https://auth.test/token")
    monkeypatch.setenv("PROJECTOPS_KC_CLIENT_ID", "project-ops")
    monkeypatch.setenv("PROJECTOPS_KC_CLIENT_SECRET", "secret")
    c2 = ProjectOpsClient()
    assert c2.auth_mode == "auto-refresh"  # auto wins even with API_KEY set


@pytest.mark.asyncio
async def test_source_lifecycle_requires_workspace_bearer_and_sends_bounded_ids():
    from services.projectops_client import (
        ProjectOpsClient,
        ProjectOpsClientError,
    )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "version": "meeting-ops.action-lifecycle.v1",
                "correlationId": "corr-client",
                "items": [],
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ProjectOpsClient(
        base_url="https://projectops.example.test/api/v1",
        _client=http,
    )

    with pytest.raises(ProjectOpsClientError):
        await client.get_source_lifecycle(["41"], bearer_override="")
    with pytest.raises(ProjectOpsClientError):
        await client.get_source_lifecycle(["41"], bearer_override="   ")

    result = await client.get_source_lifecycle(
        ["41", "41", "42"],
        bearer_override="workspace-bound-token",
    )
    assert result["items"] == []
    assert len(requests) == 1
    assert requests[0].headers["authorization"] == "Bearer workspace-bound-token"
    assert json.loads(requests[0].content) == {
        "sourceActionItemIds": ["41", "42"]
    }
    await http.aclose()


@pytest.mark.asyncio
async def test_triage_submit_accepts_workspace_bearer_without_legacy_credentials():
    from services.projectops_client import ProjectOpsClient

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"created": 1, "skipped": 0, "proposals": []},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ProjectOpsClient(
        base_url="https://projectops.example.test/api/v1",
        _client=http,
    )
    assert client.is_live is False

    result = await client.submit_candidate_action_items(
        [{"text": "Prepare evidence", "sourceActionItemId": "41"}],
        bearer_override="workspace-bound-token",
    )

    assert result["created"] == 1
    assert len(requests) == 1
    assert requests[0].url.path == "/api/v1/triage/ingest"
    assert requests[0].headers["authorization"] == "Bearer workspace-bound-token"
    await http.aclose()


@pytest.mark.asyncio
async def test_projectops_error_logs_do_not_include_remote_body_or_bearer(caplog):
    import logging

    from services.projectops_client import (
        ProjectOpsClient,
        ProjectOpsClientError,
    )

    secret_body = "meeting-content-that-must-not-be-logged"
    secret_token = "workspace-bound-secret-token"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=secret_body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ProjectOpsClient(
        base_url="https://projectops.example.test/api/v1",
        _client=http,
    )
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ProjectOpsClientError) as exc:
            await client.get_source_lifecycle(
                ["41"],
                bearer_override=secret_token,
            )

    combined = " ".join(record.getMessage() for record in caplog.records)
    assert secret_body not in combined
    assert secret_token not in combined
    assert secret_body not in str(exc.value)
    assert secret_token not in str(exc.value)
    await http.aclose()


@pytest.mark.asyncio
async def test_source_lifecycle_rejects_an_unsupported_contract_version():
    from services.projectops_client import ProjectOpsClient, ProjectOpsClientError

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"version": "wrong-contract", "items": []},
            )
        )
    )
    client = ProjectOpsClient(
        base_url="https://projectops.example.test/api/v1",
        _client=http,
    )

    with pytest.raises(ProjectOpsClientError, match="unsupported contract version"):
        await client.get_source_lifecycle(
            ["41"], bearer_override="workspace-bound-token"
        )
    await http.aclose()
