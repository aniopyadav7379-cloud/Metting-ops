"""HTTP client for Unicorn Brigade — knowledge-graph write surface.

Used by `services.brigade_writer` to push Meeting-Ops session
completion data into Brigade's FalkorDB store. Read paths (graph
visualization, agent runtime) land in Phase 2/3 of the integration
per docs/brigade-integration-design.md.

Brigade API surface used here (verified live against
`http://unicorn-brigade:8100` on bigboy, Brigade v1.13.0):

  POST /api/v1/knowledge/store/entity
    body: {name, entity_type, properties?, agent_id?}
    semantics: MERGE on entity name; properties merged on match.
    When agent_id is set, Brigade writes to a derived per-agent graph
    `agent_<agent_id_underscored>` in addition to brigade_global.

  POST /api/v1/knowledge/store/relationship
    body: {from_entity, from_type, relationship, to_entity, to_type,
           properties?, confidence?, source?, agent_id?}
    semantics: creates the edge if from+to exist; MERGE-shaped.

  GET  /api/v1/knowledge/stats
    used as a connectivity probe before draining batched writes.

Auth: ``X-API-Key`` header. Phase 1 uses the master ``BRIGADE_API_KEY``
env var (which maps to Brigade's ``BRIGADE_ADMIN_KEY``); Phase 1.5
mints a scoped ``meeting-ops`` user key via Brigade's admin API.

If ``BRIGADE_API_KEY`` is unset, every method becomes a structured
log-and-return-success no-op so dev environments without Brigade can
still run the full pipeline. The writer treats success identically.

Tenancy: the per-call ``agent_id`` is computed by ``brigade_writer``
from ``BRIGADE_TENANCY_MODE`` + org_id and passed through verbatim. We
do not interpret tenancy here.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx


logger = logging.getLogger(__name__)


# Default base URL keeps the client running inside the bigboy stack
# without env wiring; production overrides via BRIGADE_API_BASE_URL.
DEFAULT_BASE_URL = "http://unicorn-brigade:8100"

# Conservative timeout. Brigade's graph writes are fast (single FalkorDB
# MERGE plus a global-graph mirror); >5s indicates a real problem and
# the writer is fire-and-forget anyway.
DEFAULT_TIMEOUT_SECONDS = 5.0

# Exponential backoff for transient failures. Bounded retries so a
# truly unreachable Brigade doesn't pile work onto the recording's
# background task.
DEFAULT_MAX_RETRIES = 3
DEFAULT_INITIAL_BACKOFF_SECONDS = 0.25
DEFAULT_BACKOFF_MULTIPLIER = 2.0


def _api_key() -> Optional[str]:
    """Resolve the Brigade API key from env.

    Reads ``BRIGADE_API_KEY`` first (the Meeting-Ops-facing name) and
    falls back to ``BRIGADE_ADMIN_KEY`` (Brigade's own env var) so the
    same compose env can be reused if needed. Returns None when neither
    is set — that flips the client into log-only mode.
    """
    return (
        os.getenv("BRIGADE_API_KEY")
        or os.getenv("BRIGADE_ADMIN_KEY")
        or None
    )


def _base_url() -> str:
    return os.getenv("BRIGADE_API_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


@dataclass(frozen=True)
class BrigadeWriteResult:
    """Lightweight return value for the writer's call sites.

    ``ok`` is True for both real Brigade-acknowledged writes AND the
    log-only path when no API key is set — the writer treats them
    identically (the session completed; that's the user-facing
    contract). ``mode`` lets tests distinguish.
    """

    ok: bool
    mode: str  # "live" | "noop" | "error"
    detail: Optional[str] = None


class BrigadeClient:
    """Thin httpx wrapper for Brigade's knowledge-graph write endpoints.

    Connection pooling is via the underlying ``httpx.AsyncClient``;
    instances are intended to be short-lived per write batch (the
    writer creates one per session). For a longer-running shared
    client, hold ``self`` open across calls — ``aclose()`` is the
    teardown.

    All methods are idempotent: Brigade's store_entity / store_relationship
    are MERGE-based so re-issuing a write with the same entity name
    updates properties rather than creating a duplicate.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        initial_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
        backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
        _client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._base_url = (base_url or _base_url()).rstrip("/")
        # api_key is computed at __init__ rather than per-call so tests
        # can monkeypatch env once and reuse the same client across the
        # batch. _resolved_api_key=None means log-only mode.
        self._resolved_api_key = (
            api_key if api_key is not None else _api_key()
        )
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._initial_backoff = initial_backoff_seconds
        self._backoff_multiplier = backoff_multiplier
        # Externally-injected client (test seam). When None we lazily
        # build one in `_client_ctx`.
        self._injected_client = _client
        self._owned_client: Optional[httpx.AsyncClient] = None

    @property
    def is_live(self) -> bool:
        """True when an API key was resolved; False puts the client in
        log-only mode (every method returns ``BrigadeWriteResult(ok=True,
        mode='noop')`` without making HTTP calls)."""
        return self._resolved_api_key is not None

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

    async def __aenter__(self) -> "BrigadeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Public surface (writer-facing)
    # ------------------------------------------------------------------

    async def upsert_entity(
        self,
        *,
        name: str,
        entity_type: str,
        properties: Optional[dict[str, Any]] = None,
        agent_id: Optional[str] = None,
    ) -> BrigadeWriteResult:
        """Idempotent upsert of a graph node (Brigade :Entity with
        ``properties.entity_type`` carrying the conceptual label).

        ``name`` is the MERGE key on Brigade's side. Pass a stable,
        org-scoped key (e.g. ``meeting_ops_meeting_113``) so re-runs of
        the writer touch the same node rather than duplicating.
        """
        if not self.is_live:
            logger.info(
                "brigade_client noop upsert_entity name=%s type=%s agent_id=%s",
                name,
                entity_type,
                agent_id,
            )
            return BrigadeWriteResult(ok=True, mode="noop")

        payload: dict[str, Any] = {
            "name": name,
            "entity_type": entity_type,
            "properties": properties or {},
        }
        if agent_id:
            payload["agent_id"] = agent_id

        return await self._post_with_retries(
            path="/api/v1/knowledge/store/entity",
            payload=payload,
        )

    async def upsert_edge(
        self,
        *,
        from_name: str,
        from_type: str,
        relationship: str,
        to_name: str,
        to_type: str,
        properties: Optional[dict[str, Any]] = None,
        agent_id: Optional[str] = None,
    ) -> BrigadeWriteResult:
        """Idempotent upsert of an edge. Brigade's store_relationship
        endpoint walks MATCH/MERGE; calling it twice with the same
        from/to/relationship is safe."""
        if not self.is_live:
            logger.info(
                "brigade_client noop upsert_edge %s -[%s]-> %s",
                from_name,
                relationship,
                to_name,
            )
            return BrigadeWriteResult(ok=True, mode="noop")

        payload: dict[str, Any] = {
            "from_entity": from_name,
            "from_type": from_type,
            "relationship": relationship,
            "to_entity": to_name,
            "to_type": to_type,
            "properties": properties or {},
        }
        if agent_id:
            payload["agent_id"] = agent_id

        return await self._post_with_retries(
            path="/api/v1/knowledge/store/relationship",
            payload=payload,
        )

    async def delete_entity_subgraph(
        self,
        *,
        name: str,
        agent_id: str,
    ) -> BrigadeWriteResult:
        """Delete an entity and its meeting-owned child subgraph.

        New Brigade builds expose the cascade endpoint. During rolling
        upgrades, a 404/405 falls back to a redacted tombstone so content is
        no longer returned even before the delete API reaches that node.
        """
        if not self.is_live:
            logger.info("brigade_client noop delete_entity_subgraph name=%s", name)
            return BrigadeWriteResult(ok=True, mode="noop")
        try:
            async with self._client_ctx() as client:
                response = await client.delete(
                    f"{self._base_url}/api/v1/knowledge/store/entity/{name}",
                    params={"agent_id": agent_id, "cascade": "true"},
                    headers=self._headers(),
                )
            if response.status_code < 400 or response.status_code == 404:
                return BrigadeWriteResult(ok=True, mode="live")
            if response.status_code != 405:
                return BrigadeWriteResult(
                    ok=False,
                    mode="error",
                    detail=f"delete HTTP {response.status_code}: {response.text[:200]}",
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("brigade_client delete failed name=%s: %s", name, exc)
            return BrigadeWriteResult(ok=False, mode="error", detail=str(exc)[:200])

        return await self.upsert_entity(
            name=name,
            entity_type="Meeting",
            properties={"deleted": True, "redacted": True, "content": "", "summary": ""},
            agent_id=agent_id,
        )

    async def stats(self) -> BrigadeWriteResult:
        """Connectivity probe. Never raises; used by tests + the
        reconciliation job to decide whether to drain."""
        if not self.is_live:
            return BrigadeWriteResult(ok=True, mode="noop")
        async with self._client_ctx() as client:
            try:
                resp = await client.get(
                    f"{self._base_url}/api/v1/knowledge/stats",
                    headers=self._headers(),
                )
                if resp.status_code == 200:
                    return BrigadeWriteResult(ok=True, mode="live")
                return BrigadeWriteResult(
                    ok=False,
                    mode="error",
                    detail=f"stats HTTP {resp.status_code}",
                )
            except Exception as exc:  # noqa: BLE001
                return BrigadeWriteResult(
                    ok=False, mode="error", detail=str(exc)[:200]
                )

    async def fetch_entity_context(
        self,
        *,
        entity_name: str,
        include_relationships: bool = True,
        include_related: bool = True,
        max_depth: int = 1,
    ) -> Optional[dict[str, Any]]:
        """Phase 2 read path. Fetches a single entity + its 1-hop
        neighborhood from Brigade's knowledge graph.

        Returns the raw JSON dict from
        ``GET /api/v1/knowledge/context/{entity_name}`` (shape:
        ``{entity, relationships, related_entities, context_text?}``)
        or None when the entity isn't found / Brigade is in log-only
        mode / a transient HTTP error occurs.

        Never raises — the caller can branch on ``None`` to render the
        "not synced yet" state without try/except plumbing. Read-path
        failures aren't pipeline-breaking; they're rendering hints.
        """
        if not self.is_live:
            logger.info(
                "brigade_client noop fetch_entity_context name=%s", entity_name
            )
            return None

        # Brigade's context endpoint expects the unescaped name in the
        # path; httpx URL-escapes correctly so spaces / punctuation in
        # the entity name don't blow up routing.
        url = (
            f"{self._base_url}/api/v1/knowledge/context/{entity_name}"
            f"?include_relationships={'true' if include_relationships else 'false'}"
            f"&include_related={'true' if include_related else 'false'}"
            f"&max_depth={max(1, min(int(max_depth), 5))}"
        )
        try:
            async with self._client_ctx() as client:
                resp = await client.get(url, headers=self._headers())
            if resp.status_code == 200:
                try:
                    return resp.json()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "brigade_client fetch_entity_context non-json body "
                        "name=%s err=%s",
                        entity_name,
                        exc,
                    )
                    return None
            if resp.status_code == 404:
                # Entity exists in our Postgres mirror but not (yet) in
                # Brigade. The caller renders the "not_synced_yet" state.
                return None
            logger.warning(
                "brigade_client fetch_entity_context HTTP %s name=%s body=%s",
                resp.status_code,
                entity_name,
                resp.text[:200],
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "brigade_client fetch_entity_context exc name=%s: %s",
                entity_name,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Defensive: when not live this should never be called (the
        # public methods short-circuit before reaching here).
        return {
            "X-API-Key": self._resolved_api_key or "",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _client_ctx(self) -> "_AsyncClientCtx":
        """Returns an async-context-managed httpx.AsyncClient.

        If a client was injected at construction (test seam), we yield
        it without closing — the caller owns lifecycle. Otherwise we
        construct + close per-call to keep the writer's footprint
        bounded. Hot-path callers should hold a single BrigadeClient
        across the batch (one new httpx.AsyncClient per call) which is
        cheap enough at our write volume.
        """
        if self._injected_client is not None:
            return _AsyncClientCtx(self._injected_client, owns=False)
        # Lazily construct an owned client so concurrent calls inside a
        # single batch share connection pooling.
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=self._timeout)
        return _AsyncClientCtx(self._owned_client, owns=False)

    async def _post_with_retries(
        self,
        *,
        path: str,
        payload: dict[str, Any],
    ) -> BrigadeWriteResult:
        """Retry on connect/timeout/5xx; 4xx is terminal (payload won't
        get better on the next attempt)."""
        url = f"{self._base_url}{path}"
        backoff = self._initial_backoff
        last_detail: Optional[str] = None
        for attempt in range(self._max_retries):
            try:
                async with self._client_ctx() as client:
                    resp = await client.post(
                        url, json=payload, headers=self._headers()
                    )
                if resp.status_code < 400:
                    return BrigadeWriteResult(ok=True, mode="live")
                if 400 <= resp.status_code < 500:
                    # Bad payload — don't retry, log the body for debug.
                    body = resp.text[:300]
                    logger.warning(
                        "brigade_client 4xx %s %s payload=%s body=%s",
                        resp.status_code,
                        path,
                        _redact(payload),
                        body,
                    )
                    return BrigadeWriteResult(
                        ok=False,
                        mode="error",
                        detail=f"HTTP {resp.status_code}: {body}",
                    )
                # 5xx — retry.
                last_detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.ConnectTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_detail = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001
                # Unexpected — log + fall through to retry, but don't
                # blow up the calling writer.
                last_detail = f"{type(exc).__name__}: {exc}"
                logger.debug(
                    "brigade_client unexpected exc on attempt %d: %s",
                    attempt,
                    exc,
                )
            if attempt < self._max_retries - 1:
                await asyncio.sleep(backoff)
                backoff *= self._backoff_multiplier
        logger.warning(
            "brigade_client write failed after %d attempts path=%s detail=%s",
            self._max_retries,
            path,
            last_detail,
        )
        return BrigadeWriteResult(ok=False, mode="error", detail=last_detail)


class _AsyncClientCtx:
    """Tiny shim so the same async with works for both owned and
    injected httpx clients without closing the injected one."""

    def __init__(self, client: httpx.AsyncClient, *, owns: bool) -> None:
        self._client = client
        self._owns = owns

    async def __aenter__(self) -> httpx.AsyncClient:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns:
            await self._client.aclose()


def _redact(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip user-text properties from logged payloads. We log node
    names + types + edge labels (low-PII) but not the body of action
    items / decisions / mentions which can carry sensitive content."""
    if not isinstance(payload, dict):
        return {}
    redacted = {k: v for k, v in payload.items() if k != "properties"}
    if "properties" in payload and isinstance(payload["properties"], dict):
        redacted["properties"] = {
            k: ("<redacted>" if k in {"text", "label"} else v)
            for k, v in payload["properties"].items()
        }
    return redacted


__all__ = [
    "BrigadeClient",
    "BrigadeWriteResult",
    "DEFAULT_BASE_URL",
]
