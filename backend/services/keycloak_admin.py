"""Keycloak admin helper — delete a user's SSO identity (right-to-erasure).

Account self-delete (``DELETE /api/auth/me``) wipes every local row plus the
best-effort Qdrant/media purge, but on SSO deployments the user's KEYCLOAK
identity survives — the next OIDC login auto-re-provisions a fresh, empty
account (``AuthService.get_or_create_sso_user`` keys purely on email). True
right-to-erasure means the IdP identity goes too, so this module deletes the
Keycloak user via the Admin REST API.

Flow (client-credentials grant — NOT password grant; no human credential is
ever stored):

    1. ``POST {base}/realms/{realm}/protocol/openid-connect/token``
       (``grant_type=client_credentials``) with the admin client id/secret
    2. ``GET  {base}/admin/realms/{realm}/users?email={email}&exact=true``
    3. ``DELETE {base}/admin/realms/{realm}/users/{kc_user_id}``

CONFIG (env):
    KEYCLOAK_ADMIN_BASE          admin-API base URL, e.g. https://auth.example.com
                                 (falls back to the existing KEYCLOAK_URL — the
                                 customer compose already sets that on the
                                 backend; a separate ADMIN_BASE override exists
                                 because the admin API may only be reachable on
                                 an internal URL, e.g. behind Cloudflare)
    KEYCLOAK_REALM               realm name (existing setting; default "uchub")
    KEYCLOAK_ADMIN_CLIENT_ID     confidential client with a service account
    KEYCLOAK_ADMIN_CLIENT_SECRET its secret

KEYCLOAK-SIDE SETUP (operator, once per realm): create a confidential client
(e.g. ``meeting-ops-admin``) with "Client authentication" ON + "Service
accounts roles" ON and nothing else (no standard/direct-access flows), then on
its *Service accounts roles* tab assign the ``realm-management`` client role
``manage-users``. ``view-users`` is implied by ``manage-users``; do NOT grant
``realm-admin``.

FAIL-OPEN BY DESIGN: when the env isn't configured (every non-SSO/self-host
deployment, and the test suite) every call logs at info and returns ``False``
without a network hit. Any Keycloak error (timeout, 4xx/5xx, bad creds) is
swallowed, logged, and returns ``False`` — the caller's DB delete has already
committed, so identity erasure must never fail the request. The caller logs
the outcome so an operator can finish the erasure by hand if KC was down.
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# Short timeouts: this runs inline in a user-facing DELETE request.
_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


# ── config seam ────────────────────────────────────────────────────────


def _admin_base() -> str:
    """Admin-API base URL: KEYCLOAK_ADMIN_BASE, else the existing KEYCLOAK_URL."""
    raw = os.getenv("KEYCLOAK_ADMIN_BASE", "").strip() or os.getenv(
        "KEYCLOAK_URL", ""
    ).strip()
    return raw.rstrip("/")


def _realm() -> str:
    return os.getenv("KEYCLOAK_REALM", "").strip() or "uchub"


def _client_id() -> str:
    return os.getenv("KEYCLOAK_ADMIN_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "").strip()


def is_configured() -> bool:
    """True when every env piece needed to call the KC admin API is present."""
    return bool(_admin_base() and _client_id() and _client_secret())


def _client() -> httpx.AsyncClient:
    """httpx client factory — a seam so tests can swap in a MockTransport."""
    from middleware.request_context import outbound_request_headers
    return httpx.AsyncClient(timeout=_TIMEOUT, headers=outbound_request_headers())


# ── public API ─────────────────────────────────────────────────────────


async def delete_keycloak_user(email_or_sub: str) -> bool:
    """Delete the Keycloak user for ``email_or_sub``. Returns True only when
    a KC user was actually deleted; False on not-configured / not-found / any
    KC error. NEVER raises — identity erasure is best-effort by contract.

    ``email_or_sub``: an email (looked up via ``?email=&exact=true``; this is
    how SSO users are keyed locally) or a raw Keycloak user id / ``sub`` UUID
    (deleted directly, no lookup).
    """
    identifier = (email_or_sub or "").strip()
    if not identifier:
        logger.info("keycloak-admin: empty identifier; skipping SSO identity delete")
        return False

    if not is_configured():
        logger.info(
            "keycloak-admin: not configured (need KEYCLOAK_ADMIN_BASE or "
            "KEYCLOAK_URL, plus KEYCLOAK_ADMIN_CLIENT_ID + "
            "KEYCLOAK_ADMIN_CLIENT_SECRET); skipping SSO identity delete for %s",
            identifier,
        )
        return False

    base = _admin_base()
    realm = _realm()
    try:
        async with _client() as client:
            token_resp = await client.post(
                f"{base}/realms/{realm}/protocol/openid-connect/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": _client_id(),
                    "client_secret": _client_secret(),
                },
            )
            token_resp.raise_for_status()
            access_token = (token_resp.json() or {}).get("access_token") or ""
            if not access_token:
                logger.warning(
                    "keycloak-admin: token response carried no access_token; "
                    "skipping SSO identity delete for %s",
                    identifier,
                )
                return False
            auth_headers = {"Authorization": f"Bearer {access_token}"}

            if "@" in identifier:
                lookup_resp = await client.get(
                    f"{base}/admin/realms/{realm}/users",
                    params={"email": identifier, "exact": "true"},
                    headers=auth_headers,
                )
                lookup_resp.raise_for_status()
                candidates = lookup_resp.json() or []
                # exact=true should yield at most one row, but only ever
                # delete rows whose email truly matches (case-insensitive) —
                # never trust a fuzzy match for a destructive call.
                kc_ids = [
                    row["id"]
                    for row in candidates
                    if isinstance(row, dict)
                    and row.get("id")
                    and (row.get("email") or "").strip().lower()
                    == identifier.lower()
                ]
                if not kc_ids:
                    logger.info(
                        "keycloak-admin: no Keycloak user with email %s in realm "
                        "%s (local-only account, or already erased)",
                        identifier,
                        realm,
                    )
                    return False
            else:
                kc_ids = [identifier]  # already a Keycloak user id (sub)

            deleted_any = False
            for kc_id in kc_ids:
                delete_resp = await client.delete(
                    f"{base}/admin/realms/{realm}/users/{kc_id}",
                    headers=auth_headers,
                )
                if delete_resp.status_code in (200, 204):
                    logger.info(
                        "keycloak-admin: deleted Keycloak user %s (%s) from realm %s",
                        kc_id,
                        identifier,
                        realm,
                    )
                    deleted_any = True
                elif delete_resp.status_code == 404:
                    logger.info(
                        "keycloak-admin: Keycloak user %s already gone", kc_id
                    )
                else:
                    logger.warning(
                        "keycloak-admin: delete of Keycloak user %s failed: HTTP %s",
                        kc_id,
                        delete_resp.status_code,
                    )
            return deleted_any
    except Exception as exc:  # noqa: BLE001 — best-effort by contract
        logger.warning(
            "keycloak-admin: SSO identity delete failed for %s: %s", identifier, exc
        )
        return False
