"""Outbound Contact-Ops resolver — participant email -> Contact-Ops person_id.

The OUTBOUND half of federation (the inbound verifier is
``services.brigade_jwt_verifier``). Meeting-Ops stamps a participant with a
Contact-Ops ``contact_id`` so the inbound Customer-Ops reads
(``api.federation_meetings``) can find "this customer's meetings". To get that
id, MO resolves the participant's email against Contact-Ops — the contact
system-of-record — via the same Brigade RFC-8693 flow Customer-Ops uses to call
Contact-Ops:

    1. mint a ``meeting-ops`` client-credentials token (scope=contacts:read)
       from the uchub Keycloak
    2. exchange it at Brigade for an ``aud=contact-ops``, workspace-bound token
       (Brigade preserves the scope as the ``scopes`` claim)
    3. call Contact-Ops MCP ``find_person_by_identifier`` (namespace=email).
       Contact-Ops maps contacts:read -> person:read + the CLIENT role, so the
       read is authorized.

Proven live 2026-06-05 (meeting-ops -> Brigade -> CO find_person_by_identifier
-> 200). The meeting-ops KC client + ``meeting-ops`` Brigade actor grant are the
identity foundation (Stage A).

DORMANT-SAFE: when ``CONTACT_OPS_RESOLVER_ENABLED`` is false (default) or the
required env is unset, every call returns ``None`` without a network hit. Any
error (mint/exchange/MCP failure, ambiguous match) also returns ``None`` — a
resolver miss must never break finalize. Tokens are cached ~30s before exp.

JWT note: this module does not decode tokens (it only forwards them); the rest
of the backend uses python-jose, not PyJWT — keep that if decode is ever added.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

# Token cache: key -> (token, exp_epoch). 'subject' = the MO client-creds token;
# 'co:<workspace>' = the Brigade-exchanged contact-ops token per workspace.
_token_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()
_REFRESH_SKEW = 30.0
_TIMEOUT = 8.0


# ── config seam ────────────────────────────────────────────────────────


def _enabled() -> bool:
    raw = os.getenv("CONTACT_OPS_RESOLVER_ENABLED", "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


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


def _contact_ops_mcp_url() -> str:
    return os.getenv(
        "CONTACT_OPS_MCP_URL", "https://mcp.contacts.magicunicorn.dev/mcp"
    ).strip()


def _resolver_scope() -> str:
    # contacts:read is an OPTIONAL client scope on the meeting-ops KC client, so
    # it must be requested explicitly; CO maps it to person:read + CLIENT.
    return os.getenv("CONTACT_OPS_RESOLVER_SCOPE", "contacts:read").strip()


def is_configured() -> bool:
    """True when the resolver can actually run (flag on + secret present)."""
    return _enabled() and bool(_kc_client_secret())


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


async def _post_form(client: httpx.AsyncClient, url: str, data: dict, label: str) -> Optional[dict]:
    try:
        r = await client.post(
            url, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    except httpx.HTTPError as exc:
        logger.warning("resolver %s transport error: %s", label, exc)
        return None
    if r.status_code != 200:
        logger.warning("resolver %s HTTP %s", label, r.status_code)
        return None
    try:
        return r.json()
    except ValueError:
        logger.warning("resolver %s non-JSON response", label)
        return None


async def _subject_token(client: httpx.AsyncClient) -> Optional[str]:
    hit = _cached("subject")
    if hit:
        return hit
    j = await _post_form(client, _kc_token_url(), {
        "grant_type": "client_credentials",
        "client_id": _kc_client_id(),
        "client_secret": _kc_client_secret(),
        "scope": _resolver_scope(),
    }, "client-credentials mint")
    if not j or not j.get("access_token"):
        return None
    # Keycloak silently drops an OPTIONAL client scope it doesn't grant and
    # still returns 200 + a token (RFC 6749 5.1: the response `scope` lists
    # what was ACTUALLY granted). If contacts:read isn't on the meeting-ops
    # client's optional scopes, the mint looks fine here but the downstream
    # Brigade exchange / CO read fails opaquely. Surface it at the source.
    requested = _resolver_scope()
    granted = set((j.get("scope") or "").split())
    if requested and requested not in granted:
        logger.warning(
            "resolver client-credentials mint missing requested scope %r "
            "(granted=%s); the meeting-ops KC client likely does not grant it "
            "as an optional client scope — downstream contact-ops calls will 401/403",
            requested,
            sorted(granted) or "<none>",
        )
    _store("subject", j["access_token"], j.get("expires_in", 300))
    return j["access_token"]


async def _contact_ops_token(client: httpx.AsyncClient, workspace_id: str) -> Optional[str]:
    key = f"co:{workspace_id}"
    hit = _cached(key)
    if hit:
        return hit
    subject = await _subject_token(client)
    if not subject:
        return None
    j = await _post_form(client, _brigade_exchange_url(), {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token": subject,
        "audience": "contact-ops",
        "scope": _resolver_scope(),
        "workspace_id": workspace_id,
    }, "Brigade exchange")
    if not j:
        return None
    token = j.get("access_token") or j.get("token")
    if not token:
        return None
    _store(key, token, j.get("expires_in", 300))
    return token



def _structured_from_mcp_payload(payload: dict) -> Optional[dict]:
    result = payload.get("result") or {}
    if payload.get("error") or result.get("isError"):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured

    # MCP servers commonly mirror structured output as JSON text content.
    import json as _json

    for c in result.get("content", []) or []:
        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
            try:
                parsed = _json.loads(c.get("text") or "")
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    # No structuredContent dict and no parseable dict in text content — the CO
    # MCP result shape drifted from what we parse. Log the actual (truncated)
    # shape so it's diagnosable; still return None so callers don't crash.
    logger.warning(
        "resolver MCP unrecognized result shape: result_keys=%s content_types=%s payload=%.300r",
        sorted(result.keys()) if isinstance(result, dict) else type(result).__name__,
        [c.get("type") for c in (result.get("content") or []) if isinstance(c, dict)],
        payload,
    )
    return None


def _person_summary(item: Any) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    person_id = item.get("person_id") or item.get("id")
    if not person_id:
        return None
    display_name = (
        item.get("display_name")
        or item.get("name")
        or item.get("full_name")
        or item.get("email")
        or str(person_id)
    )
    email = item.get("email") or item.get("primary_email")
    if not email:
        emails = item.get("emails")
        if isinstance(emails, list) and emails:
            first = emails[0]
            if isinstance(first, str):
                email = first
            elif isinstance(first, dict):
                email = first.get("email") or first.get("value")
    raw_confidence = item.get("match_confidence")
    try:
        match_confidence = float(raw_confidence)
    except (TypeError, ValueError):
        match_confidence = 0.0
    match_confidence = max(0.0, min(match_confidence, 1.0))
    return {
        "person_id": str(person_id),
        "display_name": str(display_name) if display_name else str(person_id),
        "email": str(email) if email else None,
        "match_confidence": match_confidence,
        "match_basis": str(item.get("match_basis") or "unknown"),
    }


def _people_from_structured(structured: dict) -> list[dict]:
    candidates = None
    for key in ("people", "persons", "results", "items", "matches"):
        value = structured.get(key)
        if isinstance(value, list):
            candidates = value
            break
    if candidates is None and isinstance(structured.get("person"), dict):
        candidates = [structured["person"]]
    if candidates is None:
        candidates = []

    out: list[dict] = []
    seen: set[str] = set()
    for item in candidates:
        person = _person_summary(item)
        if not person or person["person_id"] in seen:
            continue
        seen.add(person["person_id"])
        out.append(person)
    return out


async def _call_contact_ops_tool(
    workspace_id: str,
    tool_name: str,
    arguments: dict,
    *,
    label: str,
) -> Optional[dict]:
    if not is_configured() or not workspace_id:
        return None

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        token = await _contact_ops_token(client, workspace_id)
        if not token:
            return None
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        try:
            r = await client.post(
                _contact_ops_mcp_url(),
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
            )
        except httpx.HTTPError as exc:
            logger.warning("resolver %s transport error: %s", label, exc)
            return None
        if r.status_code != 200:
            logger.warning("resolver %s HTTP %s", label, r.status_code)
            return None
        try:
            payload = r.json()
        except ValueError:
            logger.warning(
                "resolver %s 200 with non-JSON body (content-type=%s): %.200r",
                label,
                r.headers.get("content-type", "?"),
                r.text,
            )
            return None
    return _structured_from_mcp_payload(payload)

# ── public API ─────────────────────────────────────────────────────────


async def resolve_email(email: str, workspace_id: str) -> Optional[str]:
    """Resolve a participant ``email`` to a Contact-Ops person_id within
    ``workspace_id`` (the uc-registry tenant uuid — e.g.
    ``Organization.workspace_id``). Returns the person_id string, or ``None``
    when the resolver is dormant, the email is unknown, the match is ambiguous,
    or anything fails. Never raises."""
    if not is_configured():
        return None
    email = (email or "").strip().lower()
    if not email or not workspace_id:
        return None

    structured = await _call_contact_ops_tool(
        workspace_id,
        "find_person_by_identifier",
        {"identifiers": [{"namespace": "email", "value": email}]},
        label="find_person",
    )
    if not structured:
        return None
    matches = structured.get("matches") or []
    if structured.get("ambiguous") or len(matches) != 1:
        # ambiguous or no/multi match -> don't guess
        return None
    m = matches[0]
    # CO IdentifierMatchOutput spreads PersonSummary, whose id field is person_id.
    pid = m.get("person_id") or m.get("id")
    return str(pid) if pid else None


async def search_people(query: str, workspace_id: str, limit: int = 10) -> list[dict]:
    """Search Contact-Ops people by name/email for manual participant tagging.

    Returns a list of ``{person_id, display_name, email}``. Dormant config,
    missing tenant, MCP errors, or unknown response shapes return an empty list.
    Never raises.
    """
    if not is_configured():
        return []
    query = (query or "").strip()
    if not query or len(query) < 2 or not workspace_id:
        return []
    try:
        n = max(1, min(int(limit or 10), 25))
    except (TypeError, ValueError):
        n = 10

    structured = await _call_contact_ops_tool(
        workspace_id,
        "search_people_for_identity_link",
        {"query": query, "limit": n},
        label="search_people",
    )
    if not structured:
        return []
    return _people_from_structured(structured)[:n]


async def resolve_person_id(person_id: str, workspace_id: str) -> Optional[dict]:
    """Resolve a stale Contact-Ops id to its workspace-bound canonical id.

    Returns the minimal ``resolve_person_id`` contract, or ``None`` when the
    resolver is dormant/unavailable. Callers must not treat an unavailable
    resolver as proof that an id is valid.
    """

    if not is_configured():
        return None
    person_id = (person_id or "").strip()
    workspace_id = (workspace_id or "").strip()
    if not person_id or not workspace_id:
        return None
    structured = await _call_contact_ops_tool(
        workspace_id,
        "resolve_person_id",
        {"person_id": person_id},
        label="resolve_person_id",
    )
    if not structured:
        return None
    status = structured.get("status")
    if status not in {"canonical", "redirected", "not_found", "not_linkable"}:
        return None
    canonical = structured.get("canonical_person_id")
    aliases = structured.get("alias_ids")
    if not isinstance(aliases, list):
        aliases = []
    return {
        "requested_person_id": str(
            structured.get("requested_person_id") or person_id
        ),
        "canonical_person_id": str(canonical) if canonical else None,
        "alias_ids": [str(alias) for alias in aliases if alias],
        "status": status,
    }
