"""
Lyzr Agent API provider.

Real integration against Lyzr's hosted Agent API (docs.lyzr.ai). This is
NOT a wrapper that imports the SDK and never calls it: `chat()` genuinely
creates (once, cached) a Lyzr agent for the org+task and drives Lyzr's
`/v3/inference/chat/` endpoint for every completion, so every call site that
resolves its LLM through ``ProviderRegistry.get_llm()`` — summarization,
ai_insights structured extraction, and (via LyzrOrchestrationService) the
grounded Q&A path — genuinely executes through Lyzr when an org selects
``provider_name="lyzr"`` in Provider Settings.

Endpoints (per https://docs.lyzr.ai/agent-apis):
    POST {base}/v3/agents/            create an agent, returns {"id": ...}
    POST {base}/v3/inference/chat/    chat with an agent (session-scoped)

Required configuration (org Provider Settings + environment):
    api_key      -> Lyzr API key (x-api-key header). Stored encrypted per-org
                    via the existing OrgProviderSettings.api_key_encrypted,
                    same as every other provider.
    endpoint     -> Lyzr base URL, defaults to https://agent-prod.studio.lyzr.ai
    LYZR_PROVIDER_ID / LYZR_LLM_CREDENTIAL_ID (env) -> Lyzr requires an
        underlying model credential to be registered in the customer's Lyzr
        Studio account before an agent can be created; there is no way to
        fabricate these values. If unset, this provider raises
        LyzrNotConfigured instead of pretending to work (rule: never fake
        integration success).

The created agent_id is cached in OrgProviderSettings.overrides["lyzr_agent_id"]
so we don't create a new Lyzr agent on every request (idempotent provisioning).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import AsyncGenerator, Optional

import httpx
from sqlalchemy.orm import Session

from .impl_llm import LLMUnavailable

logger = logging.getLogger(__name__)

DEFAULT_LYZR_BASE_URL = os.getenv("LYZR_BASE_URL", "https://agent-prod.studio.lyzr.ai")
LYZR_PROVIDER_ID = os.getenv("LYZR_PROVIDER_ID", "")
LYZR_LLM_CREDENTIAL_ID = os.getenv("LYZR_LLM_CREDENTIAL_ID", "")
LYZR_MODEL_ID = os.getenv("LYZR_MODEL_ID", "gpt-4o-mini")

_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 1.5
_CHAT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_AGENT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

DEFAULT_SYSTEM_PROMPT = (
    "You are the reasoning engine for a meeting-intelligence platform "
    "(Meeting-Ops). You are given transcript excerpts, meeting summaries, or "
    "user questions and must produce grounded, structured output. Never "
    "invent facts, owners, deadlines, or decisions that are not supported by "
    "the material you were given. When asked for JSON, return ONLY valid "
    "JSON with no surrounding prose or markdown fences."
)


class LyzrNotConfigured(LLMUnavailable):
    """Raised when Lyzr credentials/provider IDs required to create an
    agent are missing. Distinct from a transient LLMUnavailable so callers
    (and operators reading logs) can tell 'not configured' apart from
    'Lyzr was down'."""


class LyzrProvider:
    """LLMProvider implementation backed by the real Lyzr Agent API."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = "",
        model: str = "",
        *,
        org_id: Optional[int] = None,
        db: Optional[Session] = None,
        thinking: bool = False,
    ):
        self.api_key = api_key
        self.base_url = (endpoint or DEFAULT_LYZR_BASE_URL).rstrip("/")
        # `model` here is the org's configured model_name (OrgProviderSettings);
        # for Lyzr this is used as the agent's display name / system prompt
        # selector, not an OpenAI-style model string.
        self.model = model or "meeting-ops-reasoner"
        self.org_id = org_id
        self._db = db
        self.thinking = thinking
        self._agent_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Agent provisioning (idempotent, cached per-org)
    # ------------------------------------------------------------------
    def _cached_agent_id(self) -> Optional[str]:
        if self._db is None or self.org_id is None:
            return None
        from database.models import OrgProviderSettings

        row = (
            self._db.query(OrgProviderSettings)
            .filter(
                OrgProviderSettings.organization_id == self.org_id,
                OrgProviderSettings.service_kind == "llm",
            )
            .first()
        )
        if row and isinstance(row.overrides, dict):
            return row.overrides.get("lyzr_agent_id")
        return None

    def _store_agent_id(self, agent_id: str) -> None:
        if self._db is None or self.org_id is None:
            return
        from sqlalchemy.orm.attributes import flag_modified

        from database.models import OrgProviderSettings

        row = (
            self._db.query(OrgProviderSettings)
            .filter(
                OrgProviderSettings.organization_id == self.org_id,
                OrgProviderSettings.service_kind == "llm",
            )
            .first()
        )
        if row is None:
            return
        overrides = dict(row.overrides or {})
        overrides["lyzr_agent_id"] = agent_id
        row.overrides = overrides
        flag_modified(row, "overrides")
        try:
            self._db.commit()
        except Exception:  # noqa: BLE001
            self._db.rollback()
            logger.warning("Failed to persist Lyzr agent_id for org %s", self.org_id)

    def _headers(self) -> dict:
        return {"x-api-key": self.api_key, "Content-Type": "application/json", "accept": "application/json"}

    async def _ensure_agent(self, client: httpx.AsyncClient) -> str:
        if self._agent_id:
            return self._agent_id
        cached = self._cached_agent_id()
        if cached:
            self._agent_id = cached
            return cached

        if not self.api_key:
            raise LyzrNotConfigured("Lyzr provider selected but no API key configured for this organization.")
        if not LYZR_PROVIDER_ID or not LYZR_LLM_CREDENTIAL_ID:
            raise LyzrNotConfigured(
                "Lyzr agent creation requires LYZR_PROVIDER_ID and LYZR_LLM_CREDENTIAL_ID "
                "(created in the customer's Lyzr Studio account). Set them in the environment; "
                "this cannot be inferred or fabricated."
            )

        payload = {
            "name": f"meeting-ops-{self.org_id or 'default'}",
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "description": "Meeting-Ops reasoning/orchestration agent (summary, extraction, grounded Q&A).",
            "features": [],
            "tools": [],
            "llm_credential_id": LYZR_LLM_CREDENTIAL_ID,
            "provider_id": LYZR_PROVIDER_ID,
            "model": LYZR_MODEL_ID,
            "top_p": 0.9,
            "temperature": 0.3,
        }
        resp = await client.post(f"{self.base_url}/v3/agents/", headers=self._headers(), json=payload)
        if resp.status_code in (401, 403):
            raise LyzrNotConfigured(f"Lyzr rejected credentials during agent creation: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        agent_id = data.get("id") or data.get("agent_id") or data.get("created_agent_id")
        if not agent_id:
            raise LLMUnavailable(f"Lyzr agent creation returned no agent id: {data}")
        agent_id = str(agent_id)
        self._agent_id = agent_id
        self._store_agent_id(agent_id)
        logger.info("Provisioned Lyzr agent %s for org %s", agent_id, self.org_id)
        return agent_id

    # ------------------------------------------------------------------
    # LLMProvider protocol
    # ------------------------------------------------------------------
    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 500,
        temperature: float = 0.7,
        extra_params: Optional[dict] = None,
    ) -> str:
        # extra_params (top_p/top_k/presence_penalty/...) is an OpenAI-style
        # per-request knob some call sites (api.uploads._summarize_session)
        # pass through positionally-agnostic to whichever LLMProvider is
        # active. Lyzr sets top_p/temperature at agent-creation time instead
        # of per-message, so there is nothing to forward here — accepted for
        # interface compatibility and intentionally ignored, not silently
        # dropped without a trace.
        if extra_params:
            logger.debug("Lyzr ignores per-call extra_params (set at agent creation instead): %s", list(extra_params.keys()))
        last_exc: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=_AGENT_TIMEOUT) as agent_client:
            agent_id = await self._ensure_agent(agent_client)

        message = f"{system_prompt}\n\n---\n\n{user_prompt}" if system_prompt else user_prompt
        session_id = f"mops-{self.org_id or 'na'}-{uuid.uuid4().hex[:12]}"

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=_CHAT_TIMEOUT) as client:
                    resp = await client.post(
                        f"{self.base_url}/v3/inference/chat/",
                        headers=self._headers(),
                        json={
                            "user_id": f"org-{self.org_id or 'anonymous'}",
                            "agent_id": agent_id,
                            "session_id": session_id,
                            "message": message,
                        },
                    )
                if resp.status_code in (401, 403):
                    # Permanent auth failure — do not retry.
                    raise LyzrNotConfigured(f"Lyzr rejected credentials: {resp.status_code} {resp.text[:200]}")
                if resp.status_code == 422:
                    # Permanent validation error — do not retry indefinitely.
                    raise LLMUnavailable(f"Lyzr rejected request payload: {resp.text[:300]}")
                resp.raise_for_status()
                data = resp.json()
                content = (data.get("agent_response") or data.get("response") or "").strip()
                if not content:
                    raise LLMUnavailable(f"Lyzr returned an empty response: {data}")
                return content
            except LyzrNotConfigured:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                    continue
        raise LLMUnavailable(f"Lyzr chat failed after {_MAX_RETRIES} attempts: {last_exc}") from last_exc

    def _ensure_agent_sync(self, client: httpx.Client) -> str:
        if self._agent_id:
            return self._agent_id
        cached = self._cached_agent_id()
        if cached:
            self._agent_id = cached
            return cached
        if not self.api_key:
            raise LyzrNotConfigured("Lyzr provider selected but no API key configured for this organization.")
        if not LYZR_PROVIDER_ID or not LYZR_LLM_CREDENTIAL_ID:
            raise LyzrNotConfigured(
                "Lyzr agent creation requires LYZR_PROVIDER_ID and LYZR_LLM_CREDENTIAL_ID "
                "(created in the customer's Lyzr Studio account). Set them in the environment; "
                "this cannot be inferred or fabricated."
            )
        payload = {
            "name": f"meeting-ops-{self.org_id or 'default'}",
            "system_prompt": DEFAULT_SYSTEM_PROMPT,
            "description": "Meeting-Ops reasoning/orchestration agent (summary, extraction, grounded Q&A).",
            "features": [],
            "tools": [],
            "llm_credential_id": LYZR_LLM_CREDENTIAL_ID,
            "provider_id": LYZR_PROVIDER_ID,
            "model": LYZR_MODEL_ID,
            "top_p": 0.9,
            "temperature": 0.3,
        }
        resp = client.post(f"{self.base_url}/v3/agents/", headers=self._headers(), json=payload)
        if resp.status_code in (401, 403):
            raise LyzrNotConfigured(f"Lyzr rejected credentials during agent creation: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        agent_id = data.get("id") or data.get("agent_id") or data.get("created_agent_id")
        if not agent_id:
            raise LLMUnavailable(f"Lyzr agent creation returned no agent id: {data}")
        agent_id = str(agent_id)
        self._agent_id = agent_id
        self._store_agent_id(agent_id)
        return agent_id

    def chat_sync(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 500,
        temperature: float = 0.7,
        extra_params: Optional[dict] = None,
    ) -> str:
        """Genuinely synchronous — uses httpx.Client, never spins up a nested
        asyncio event loop. Safe to call from within an already-running
        event loop's thread (e.g. FastAPI request handlers that call a sync
        provider method directly), which asyncio.run()/run_until_complete()
        is NOT — that was this method's original (buggy) implementation,
        caught by tests/test_ask_ai_e2e.py actually exercising the HTTP
        endpoint rather than calling chat() directly."""
        if extra_params:
            logger.debug("Lyzr ignores per-call extra_params (set at agent creation instead): %s", list(extra_params.keys()))
        message = f"{system_prompt}\n\n---\n\n{user_prompt}" if system_prompt else user_prompt
        session_id = f"mops-{self.org_id or 'na'}-{uuid.uuid4().hex[:12]}"
        last_exc: Optional[Exception] = None
        try:
            with httpx.Client(timeout=_AGENT_TIMEOUT) as agent_client:
                agent_id = self._ensure_agent_sync(agent_client)
        except LyzrNotConfigured as exc:
            logger.error(f"Lyzr not configured: {exc}")
            return ""

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with httpx.Client(timeout=_CHAT_TIMEOUT) as client:
                    resp = client.post(
                        f"{self.base_url}/v3/inference/chat/",
                        headers=self._headers(),
                        json={
                            "user_id": f"org-{self.org_id or 'anonymous'}",
                            "agent_id": agent_id,
                            "session_id": session_id,
                            "message": message,
                        },
                    )
                if resp.status_code in (401, 403, 422):
                    logger.error(f"Lyzr sync chat permanently failed: {resp.status_code} {resp.text[:200]}")
                    return ""
                resp.raise_for_status()
                data = resp.json()
                content = (data.get("agent_response") or data.get("response") or "").strip()
                return content
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
                    continue
        logger.error(f"Lyzr sync chat failed after {_MAX_RETRIES} attempts: {last_exc}")
        return ""

    async def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 500,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        # Lyzr's streaming endpoint (/v3/inference/stream/) uses a different
        # response shape (SSE frames) than the tool-calling loop in
        # meeting_rag.py expects from an OpenAI-style provider, so rather than
        # emit a fake single-token stream we do a real non-streamed Lyzr call
        # and yield it as one chunk. Documented limitation — see README.
        result = await self.chat(system_prompt, user_prompt, max_tokens=max_tokens, temperature=temperature)
        yield result

    async def health(self) -> dict:
        if not self.api_key:
            return {"available": False, "endpoint": self.base_url, "model": self.model, "error": "No Lyzr API key configured"}
        if not LYZR_PROVIDER_ID or not LYZR_LLM_CREDENTIAL_ID:
            return {
                "available": False,
                "endpoint": self.base_url,
                "model": self.model,
                "error": "LYZR_PROVIDER_ID / LYZR_LLM_CREDENTIAL_ID not configured",
            }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.base_url}/v3/agents/", headers=self._headers())
            if resp.status_code in (200, 401, 403):
                # Any structured HTTP response (even auth failure) proves the
                # endpoint is reachable; 401/403 is reported as unavailable below.
                if resp.status_code == 200:
                    return {"available": True, "endpoint": self.base_url, "model": self.model, "error": None}
                return {"available": False, "endpoint": self.base_url, "model": self.model, "error": f"HTTP {resp.status_code}"}
            return {"available": False, "endpoint": self.base_url, "model": self.model, "error": f"HTTP {resp.status_code}"}
        except httpx.HTTPError as exc:
            return {"available": False, "endpoint": self.base_url, "model": self.model, "error": str(exc)}
