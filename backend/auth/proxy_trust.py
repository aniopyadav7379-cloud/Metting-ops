"""Proxy-trust boundary for SSO forward-auth identity headers.

oauth2-proxy authenticates the user against Keycloak and Traefik forwards the
resolved identity to this backend as plaintext request headers
(``X-Auth-Request-Email`` / ``-Groups`` / ``-Preferred-Username`` / ...). On a
shared Docker network those headers are trivially forgeable: any *other*
container can open a socket straight to this backend (bypassing Traefik) and
set them itself. Because ``X-Auth-Request-Groups`` drives ``is_superuser`` at
auto-provision time, an unguarded trust of these headers is a full auth bypass
*and* a privilege escalation to global admin.

This module gates that trust on a per-deploy shared secret that ONLY the
fronting proxy knows and injects (``X-Proxy-Auth``). The backend honours the
forward-auth identity headers only when the request also carries the matching
secret.

The trust gate is **FAIL-CLOSED by default**:

  * **No secret configured → headers IGNORED.** If ``PROXY_AUTH_SHARED_SECRET``
    is unset/empty, the forward-auth identity headers are never honoured. This
    is correct (and safe) for deployments that don't use oauth2-proxy
    forward-auth at all — e.g. the native-OIDC prod path, which authenticates
    via the ``mo_uc_session`` cookie. A forged ``X-Auth-Request-*`` on such a
    deploy can no longer authenticate anyone.
  * **Secret configured → headers honoured only with a matching X-Proxy-Auth.**
    A request whose ``X-Proxy-Auth`` is missing or non-matching has its
    forward-auth headers ignored entirely. The caller then falls through to
    JWT / UC-SSO cookie / PAT / API-key auth — all cryptographically
    authenticated and unaffected by this gate — so a legitimately logged-in
    user is never locked out.

Trusted ⟺ (secret configured) AND (request carries a matching ``X-Proxy-Auth``).
An oauth2-proxy / forward-auth deployment therefore MUST set the secret AND wire
the Traefik ``X-Proxy-Auth`` injection (``customRequestHeaders``, which also
overwrites any client-supplied copy, so a forger cannot smuggle the header
through the proxy). As defence-in-depth the edge should also strip inbound
``X-Auth-Request-*`` headers. The secret is compared in constant time
(``secrets.compare_digest``), mirroring ``auth/internal.py``.

``forward_auth_trusted`` accepts any case-insensitive header mapping, so it
works for both Starlette ``Request.headers`` and ``WebSocket.headers``.
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Mapping

logger = logging.getLogger(__name__)

# Secret-carrying header the fronting proxy (Traefik) injects on trusted routes.
PROXY_AUTH_HEADER = "X-Proxy-Auth"


def _configured_secret() -> str:
    return os.environ.get("PROXY_AUTH_SHARED_SECRET", "") or ""


def proxy_trust_enforced() -> bool:
    """True when a shared secret is configured.

    The trust gate is fail-closed regardless of this value; it reports whether
    the ``X-Proxy-Auth`` handshake is provisioned (i.e. whether this deploy can
    honour forward-auth identity headers at all). False => forward-auth is
    effectively disabled and all auth flows through tokens / cookies.
    """
    return bool(_configured_secret())


def _warn_no_secret() -> None:
    # Log once per process so an unconfigured forward-auth deploy is visible in
    # ops dashboards without flooding them. For native-OIDC / cookie deploys
    # (e.g. prod) this is expected and benign.
    if not getattr(_warn_no_secret, "_warned", False):
        logger.warning(
            "PROXY_AUTH_SHARED_SECRET is unset; SSO forward-auth identity "
            "headers (X-Auth-Request-*) are IGNORED (fail-closed). This is "
            "correct for deployments that don't use oauth2-proxy forward-auth "
            "(e.g. native-OIDC). If this deployment DOES use forward-auth, set "
            "the env var AND the matching Traefik %s injection.",
            PROXY_AUTH_HEADER,
        )
        _warn_no_secret._warned = True  # type: ignore[attr-defined]


def _warn_rejected() -> None:
    # Rate-limited to once per process so a hammering forger can't flood logs.
    if not getattr(_warn_rejected, "_warned", False):
        logger.warning(
            "Ignoring SSO forward-auth identity headers on a request lacking a "
            "valid %s secret (likely a forged or direct-to-container call). The "
            "caller falls through to token auth.",
            PROXY_AUTH_HEADER,
        )
        _warn_rejected._warned = True  # type: ignore[attr-defined]


def forward_auth_trusted(headers: Mapping[str, str]) -> bool:
    """Whether the SSO forward-auth identity headers on this request are trusted.

    ``headers`` is any case-insensitive mapping (Starlette ``Request.headers``
    or ``WebSocket.headers``). Returns True when the headers may be honoured,
    False when they must be ignored.
    """
    configured = _configured_secret()
    if not configured:
        _warn_no_secret()
        return False  # fail-closed: no secret => forward-auth headers ignored

    presented = headers.get(PROXY_AUTH_HEADER)
    if not presented:
        _warn_rejected()
        return False
    # compare_digest short-circuits on unequal length; encode both sides so a
    # non-string presented value can't perturb the timing profile.
    try:
        ok = secrets.compare_digest(
            presented.encode("utf-8"), configured.encode("utf-8")
        )
    except (AttributeError, TypeError):
        ok = False
    if not ok:
        _warn_rejected()
    return ok


__all__ = ["PROXY_AUTH_HEADER", "forward_auth_trusted", "proxy_trust_enforced"]
