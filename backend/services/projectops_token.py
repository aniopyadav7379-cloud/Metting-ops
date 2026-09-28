"""Outbound Project-Ops federation token — Brigade-vouched, workspace-bound.

The OUTBOUND half of MO -> Project-Ops federation for the triage push. When
Meeting-Ops submits a meeting's action items to Project-Ops' triage inbox, the
push must carry the meeting org's tenant so PO's multi-tenancy routes the
proposals into the correct workspace. A bare service token carries NO tenant
context, so PO would mis-route under its new multi-tenancy.

This module mints a Brigade-exchanged token bound to ``audience=project-ops``
and carrying the org's ``workspace_id`` — exactly the RFC-8693 flow the
Contact-Ops resolver (``services.contact_ops_resolver``) uses for
``aud=contact-ops``:

    1. mint a ``meeting-ops`` client-credentials token (the SAME KC client +
       env as the Contact-Ops resolver) from the uchub Keycloak
    2. exchange it at Brigade for an ``aud=project-ops``, workspace-bound token
       (Brigade preserves the scope as the ``scopes`` claim). PO trusts the
       Brigade-vouched ``workspace_id``.

DORMANT-SAFE: when the KC client secret is unset, or any step fails (mint /
exchange transport, non-200, non-JSON, missing token), every call returns
``None`` WITHOUT raising — a federation-token miss must never break finalize.
The writer treats that result as a recoverable sync failure and does not send
the proposal; it never falls back to a default service tenant. Tokens are
cached per-workspace ~30s before exp, in the SAME module-level cache shape as
the resolver.

BRIGADE-CONFIG DEPENDENCY: Brigade must allow actor ``meeting-ops`` to exchange
for audience ``project-ops`` — i.e. ``project-ops`` in Brigade's
``BRIGADE_EXCHANGE_ALLOWED_AUDIENCES`` and ``meeting-ops`` in
``BRIGADE_EXCHANGE_ALLOWED_ACTORS``. Until that grant exists the exchange
returns non-200 and the writer records a recoverable sync failure without
sending the proposal.

JWT note: this module does not decode tokens (it only forwards them); the rest
of the backend uses python-jose, not PyJWT — keep that if decode is ever added.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Token cache: key -> (token, exp_epoch). 'subject' = the MO client-creds token
# (shared shape with the resolver, but a private cache here); 'po:<workspace>' =
# the Brigade-exchanged project-ops token per workspace.
_token_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()
_REFRESH_SKEW = 30.0
_TIMEOUT = 8.0


# ── config seam ────────────────────────────────────────────────────────


def _kc_token_url() -> str:
    return os.getenv(
        "MEETING_OPS_KC_TOKEN_URL",
        "https://auth.unicorncommander.ai/realms/uchub/protocol/openid-connect/token",
    ).strip()


def _kc_client_id() -> str:
    return os.getenv("MEETING_OPS_KC_CLIENT_ID", "meeting-ops").strip()


def _kc_client_secret() -> str:
    return os.getenv("MEETING_OPS_KC_CLIENT_SECRET", "").strip()


def _brigade_exchange_url() -> str:
    return os.getenv(
        "BRIGADE_EXCHANGE_URL",
        "https://brigade.unicorncommander.ai/api/v1/federation/token",
    ).strip()


def _federation_audience() -> str:
    return os.getenv("PROJECTOPS_FEDERATION_AUDIENCE", "project-ops").strip()


def _federation_scope() -> str:
    # A propose-only scope; PO doesn't strictly require it yet. Brigade
    # preserves it as the ``scopes`` claim.
    return os.getenv("PROJECTOPS_FEDERATION_SCOPE", "triage:write").strip()


def is_configured() -> bool:
    """True when the federation exchange can actually run (secret present)."""
    return bool(_kc_client_secret())


# ── token plumbing ─────────────────────────────────────────────────────


def _cached(key: str) -> Optional[str]:
    with _cache_lock:
        hit = _token_cache.get(key)
        if hit and hit[1] - _REFRESH_SKEW > time.time():
            return hit[0]
    return None


def _store(key: str, token: str, expires_in: float) -> None:
    with _cache_lock:
        _token_cache[key] = (token, time.time() + float(expires_in or 300))


async def _post_form(
    client: httpx.AsyncClient, url: str, data: dict, label: str
) -> Optional[dict]:
    try:
        r = await client.post(
            url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        logger.warning("projectops_token %s transport error: %s", label, exc)
        return None
    if r.status_code != 200:
        logger.warning("projectops_token %s HTTP %s", label, r.status_code)
        return None
    try:
        return r.json()
    except ValueError:
        logger.warning("projectops_token %s non-JSON response", label)
        return None


async def _subject_token(client: httpx.AsyncClient) -> Optional[str]:
    hit = _cached("subject")
    if hit:
        return hit
    j = await _post_form(client, _kc_token_url(), {
        "grant_type": "client_credentials",
        "client_id": _kc_client_id(),
        "client_secret": _kc_client_secret(),
        "scope": _federation_scope(),
    }, "client-credentials mint")
    if not j or not j.get("access_token"):
        return None
    _store("subject", j["access_token"], j.get("expires_in", 300))
    return j["access_token"]


async def _project_ops_token(
    client: httpx.AsyncClient, workspace_id: str
) -> Optional[str]:
    key = f"po:{workspace_id}"
    hit = _cached(key)
    if hit:
        return hit
    subject = await _subject_token(client)
    if not subject:
        return None
    j = await _post_form(client, _brigade_exchange_url(), {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": subject,
        "audience": _federation_audience(),
        "scope": _federation_scope(),
        "workspace_id": workspace_id,
    }, "Brigade exchange")
    if not j:
        return None
    token = j.get("access_token") or j.get("token")
    if not isinstance(token, str) or not token.strip():
        return None
    token = token.strip()
    _store(key, token, j.get("expires_in", 300))
    return token


# ── public API ─────────────────────────────────────────────────────────


async def projectops_federation_token(workspace_id: str) -> Optional[str]:
    """Mint a Brigade-vouched, ``workspace_id``-bound token for the MO ->
    Project-Ops triage push (``audience=project-ops``, ``scope=triage:write``).

    ``workspace_id`` is the uc-registry tenant uuid (e.g.
    ``Organization.workspace_id``). Returns the access_token string, or ``None``
    when unconfigured (KC secret unset), the workspace_id is empty, or any step
    fails (mint / exchange transport, non-200, non-JSON, missing token, or
    Brigade not yet granting actor=meeting-ops -> aud=project-ops). The caller
    fails closed and performs no Project-Ops request on ``None``. NEVER raises.
    """
    if not is_configured():
        return None
    workspace_id = (workspace_id or "").strip()
    if not workspace_id:
        return None
    try:
        from middleware.request_context import outbound_request_headers
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=outbound_request_headers()) as client:
            return await _project_ops_token(client, workspace_id)
    except Exception as exc:  # noqa: BLE001
        # Defensive: a token miss must never break finalize.
        logger.warning(
            "projectops_token federation mint swallowed error ws=%s: %s",
            workspace_id,
            exc,
        )
        return None


__all__ = ["projectops_federation_token", "is_configured"]
