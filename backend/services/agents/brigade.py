"""
Brigade agent source — federates Unicorn Brigade agents into Meeting-Ops.

Auth: the Brigade ``/api/v1/agents`` read path is service-authed — it wants the
same ``X-API-Key`` service key the writer/client use, NOT a user JWT (a user
bearer 401s, which is the bug this fixes). Failures degrade to "no Brigade
agents" rather than surfacing an error in the chat picker. Caches list
responses per (org_id, user_email) for ~60s in Redis to avoid hammering
Brigade on every page load.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import Request
from sqlalchemy.orm import Session

from .base import AgentDescriptor, AgentSource

logger = logging.getLogger(__name__)

BRIGADE_URL_DEFAULT = "http://unicorn-brigade:8100"
CACHE_TTL_SECONDS = 60
CACHE_KEY_PREFIX = "meetingops:agents:brigade"
HTTP_TIMEOUT = 5.0  # short — UI must stay snappy when Brigade is down


def _brigade_base() -> str:
    return os.getenv("BRIGADE_URL", BRIGADE_URL_DEFAULT).rstrip("/")


def _brigade_api_key() -> Optional[str]:
    """Brigade service key — the same one brigade_client / brigade_writer use.
    The agents read endpoint is service-authed, so we send X-API-Key (a user
    JWT 401s)."""
    return os.getenv("BRIGADE_API_KEY") or os.getenv("BRIGADE_ADMIN_KEY") or None


def _bearer_from_request(request: Request) -> Optional[str]:
    """Extract the user's bearer JWT (oauth2-proxy injects this)."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth:
        return None
    return auth


def _user_cache_key(request: Request, org_id: int) -> str:
    user_id = (
        request.headers.get("x-auth-request-email")
        or request.headers.get("x-auth-request-preferred-username")
        or request.headers.get("x-auth-request-user")
        or "unknown"
    ).lower()
    return f"{CACHE_KEY_PREFIX}:org={org_id}:user={user_id}"


def _redis_client():
    """Best-effort Redis client. Returns None if unavailable."""
    try:
        import redis  # type: ignore
        url = os.getenv("REDIS_URL")
        if not url:
            return None
        return redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Redis unavailable for brigade cache: {e}")
        return None


def _cache_get(key: str) -> Optional[list[dict[str, Any]]]:
    r = _redis_client()
    if r is None:
        return None
    try:
        raw = r.get(key)
        if raw:
            return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Redis brigade cache get failed: {e}")
    return None


def _cache_set(key: str, value: list[dict[str, Any]]):
    r = _redis_client()
    if r is None:
        return
    try:
        r.setex(key, CACHE_TTL_SECONDS, json.dumps(value))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Redis brigade cache set failed: {e}")


def _map_brigade_agent_to_descriptor(raw: dict[str, Any]) -> AgentDescriptor:
    """Map Brigade's agent JSON shape to our AgentDescriptor."""
    real_id = str(raw.get("id") or raw.get("name") or "").strip()
    display = raw.get("displayName") or raw.get("name") or real_id
    title = raw.get("title") or ""
    bio = raw.get("bio") or raw.get("persona") or ""
    description = title.strip()
    if bio:
        description = f"{description} — {bio.strip()}" if description else bio.strip()
    if not description:
        description = display

    capabilities_raw = raw.get("capabilities")
    capabilities: Optional[list[str]] = None
    if isinstance(capabilities_raw, list):
        capabilities = [str(c) for c in capabilities_raw if c]
    elif isinstance(capabilities_raw, dict):
        capabilities = [str(k) for k in capabilities_raw.keys()]

    tools = raw.get("tools") or []
    if isinstance(tools, list) and tools:
        if not capabilities:
            capabilities = []
        tool_names = []
        for t in tools:
            if isinstance(t, dict):
                tn = t.get("name") or t.get("id")
                if tn:
                    tool_names.append(str(tn))
            elif isinstance(t, str):
                tool_names.append(t)
        if tool_names:
            capabilities.extend([f"tool:{tn}" for tn in tool_names])

    return AgentDescriptor(
        id=f"brigade:{real_id}",
        name=display,
        description=(description or display)[:500],
        source="brigade",
        domain=raw.get("domain"),
        capabilities=capabilities,
        requires_role=None,
        icon=raw.get("avatar"),
        streaming=True,
    )


class BrigadeAgentSource(AgentSource):
    name = "brigade"

    async def list_agents(
        self,
        request: Request,
        db: Session,
        org_id: int,
    ):
        api_key = _brigade_api_key()
        if not api_key:
            logger.debug("No Brigade API key configured — skipping Brigade source")
            return []

        cache_key = _user_cache_key(request, org_id)
        cached = _cache_get(cache_key)
        if cached is not None:
            return [_map_brigade_agent_to_descriptor(raw) for raw in cached]

        url = f"{_brigade_base()}/api/v1/agents"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, headers={"X-API-Key": api_key})
        except Exception as exc:  # noqa: BLE001 — network/timeout, never surface
            logger.warning("Brigade agents fetch failed (suppressed): %s", exc)
            return []
        if resp.status_code != 200:
            # Degrade to "no Brigade agents" instead of raising — a Brigade
            # hiccup must NOT dump an error into the chat agent picker.
            logger.warning(
                "Brigade /api/v1/agents returned HTTP %s (suppressed)", resp.status_code
            )
            return []

        body = resp.json()
        if isinstance(body, dict):
            raw_agents = body.get("agents") or body.get("data") or []
        elif isinstance(body, list):
            raw_agents = body
        else:
            raw_agents = []

        _cache_set(cache_key, raw_agents)

        return [_map_brigade_agent_to_descriptor(raw) for raw in raw_agents]

    async def get_agent(
        self,
        agent_id: str,
        request: Request,
        db: Session,
        org_id: int,
    ):
        if not agent_id.startswith("brigade:"):
            return None
        real_id = agent_id.split(":", 1)[1]
        api_key = _brigade_api_key()
        if not api_key:
            return None
        url = f"{_brigade_base()}/api/v1/agents/{real_id}"
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                resp = await client.get(url, headers={"X-API-Key": api_key})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Brigade get agent %s failed (suppressed): %s", real_id, exc)
            return None
        if resp.status_code != 200:
            logger.warning(f"Brigade get agent {real_id} returned {resp.status_code}")
            return None
        return _map_brigade_agent_to_descriptor(resp.json())


def brigade_agent_real_id(agent_id: str) -> Optional[str]:
    """Strip the 'brigade:' prefix; return None if not a brigade id."""
    if agent_id.startswith("brigade:"):
        return agent_id.split(":", 1)[1]
    return None
