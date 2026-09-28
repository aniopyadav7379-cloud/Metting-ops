"""Tests for the SSO forward-auth proxy-trust boundary (security-1).

``auth/proxy_trust.forward_auth_trusted`` gates whether the
``X-Auth-Request-*`` identity headers may be honoured. Those headers are
forgeable by any co-tenant container on the shared docker network, so trusting
them unconditionally is an auth bypass + superuser escalation (the groups
header drives ``is_superuser`` at auto-provision time).

The gate is FAIL-CLOSED:

  * No secret configured → forward-auth headers are IGNORED (correct for
    native-OIDC / cookie deploys that don't use forward-auth at all).
  * Secret set → a request must carry a matching ``X-Proxy-Auth`` (injected
    only by the trusted proxy) or its identity headers are ignored and the
    caller falls through to token auth.

Trusted ⟺ (secret configured) AND (matching ``X-Proxy-Auth``).

Comparison is constant-time (``secrets.compare_digest``), mirroring
``auth/internal.py``.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from starlette.datastructures import Headers

from auth.proxy_trust import PROXY_AUTH_HEADER, forward_auth_trusted, proxy_trust_enforced


SECRET = "test-proxy-secret-aaaa-bbbb-cccc-dddd-eeee-ffff-0011-2233"
WRONG = "test-proxy-secret-not-the-right-value-0123456789abcdef"


@pytest.fixture()
def with_secret():
    """Secret configured → fail-closed mode active."""
    with patch.dict(os.environ, {"PROXY_AUTH_SHARED_SECRET": SECRET}):
        yield


@pytest.fixture()
def without_secret():
    """Secret cleared → fail-closed (forward-auth headers ignored)."""
    with patch.dict(os.environ, {"PROXY_AUTH_SHARED_SECRET": ""}):
        yield


# ---------------------------------------------------------------------------
# Unit: the gate function
# ---------------------------------------------------------------------------
def test_fail_closed_when_secret_unset(without_secret):
    # No secret configured → forward-auth headers are IGNORED (fail-closed).
    # proxy_trust_enforced() still reports "no secret provisioned".
    assert proxy_trust_enforced() is False
    assert forward_auth_trusted(Headers({"X-Auth-Request-Email": "u@x.com"})) is False
    # And of course with no X-Proxy-Auth header at all.
    assert forward_auth_trusted(Headers({})) is False


def test_fail_closed_missing_proxy_header(with_secret):
    # Secret configured but request carries no X-Proxy-Auth → not trusted.
    assert proxy_trust_enforced() is True
    assert forward_auth_trusted(Headers({"X-Auth-Request-Email": "u@x.com"})) is False


def test_fail_closed_matching_secret(with_secret):
    # Correct secret (the Traefik-injected path) → trusted.
    assert forward_auth_trusted(Headers({PROXY_AUTH_HEADER: SECRET})) is True


def test_fail_closed_wrong_secret(with_secret):
    assert forward_auth_trusted(Headers({PROXY_AUTH_HEADER: WRONG})) is False


def test_header_match_is_case_insensitive(with_secret):
    # Starlette headers are case-insensitive; a lower-cased injection still matches.
    assert forward_auth_trusted(Headers({"x-proxy-auth": SECRET})) is True


def test_empty_presented_value_rejected(with_secret):
    assert forward_auth_trusted(Headers({PROXY_AUTH_HEADER: ""})) is False


# ---------------------------------------------------------------------------
# Integration: forged identity header is ignored once enforced
# ---------------------------------------------------------------------------
def test_forged_sso_header_ignored_when_enforced(client, with_secret):
    # Secret is set but we present NO X-Proxy-Auth (simulating a direct-to-
    # container forge). The X-Auth-Request-Email must be ignored, so /api/auth/me
    # — which requires a real authenticated user — must NOT return a profile.
    resp = client.get(
        "/api/auth/me",
        headers={
            "X-Auth-Request-Email": "attacker@evil.com",
            "X-Auth-Request-Groups": "uc-admins",  # would escalate to superuser
        },
    )
    assert resp.status_code != 200, (
        f"Forged forward-auth header authenticated /api/auth/me without a valid "
        f"X-Proxy-Auth secret: {resp.status_code} {resp.text}"
    )
