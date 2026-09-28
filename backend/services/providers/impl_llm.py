import json
import logging
import os
import time
from typing import Optional, AsyncGenerator

import httpx

from .protocols import LLMProvider

logger = logging.getLogger(__name__)

# Honor the deploy's configured gateway/LLM base before the in-cluster default.
# On bigboy OPENAI_API_BASE is the in-network litellm container (resolves); on
# prod (centerdeep, no such container) it's the tailnet gateway. Without this the
# org-less default_llm() path (readiness probe + background jobs) hardcodes the
# container name and fails name resolution off-cluster.
DEFAULT_LITELLM_ENDPOINT = (
    os.getenv("LITELLM_API_BASE")
    or os.getenv("OPENAI_API_BASE")
    or os.getenv("OPENAI_BASE_URL")
    or "http://unicorn-litellm:4000/v1"
)


class LLMUnavailable(RuntimeError):
    """The configured LLM could not produce a usable response."""

# External / frontier hosts that would 400 on an unknown ``chat_template_kwargs``
# field. We only ever send that field to the local OpenAI-compatible llama.cpp /
# LiteLLM gateway, never to these.
_EXTERNAL_LLM_HOSTS = (
    "api.openai.com",
    "api.anthropic.com",
    "openrouter.ai",
    "api.lambdalabs.com",
    "bedrock-runtime",
    "amazonaws.com",
    "api.cohere.ai",
    "googleapis.com",
    "api.mistral.ai",
    "api.groq.com",
    "api.together.xyz",
    "api.deepseek.com",
)


def _disable_thinking_enabled() -> bool:
    """Master kill-switch for the enable_thinking injection.

    Defaults ON (thinking suppressed) to keep Meeting-Ops's server LLM calls
    fast on the local Qwen. Set ``MEETING_OPS_DISABLE_THINKING=false`` (or 0 /
    no / off) to stop injecting the field — e.g. if a future local model
    needs its reasoning trace on by default."""
    return os.getenv("MEETING_OPS_DISABLE_THINKING", "true").strip().lower() not in (
        "false",
        "0",
        "no",
        "off",
    )


def _is_local_qwen(model: str, endpoint: str) -> bool:
    """True only when it is safe to send ``chat_template_kwargs.enable_thinking``:
    the target model is a Qwen (its chat template honors the flag) AND the
    endpoint is the local OpenAI-compatible gateway (not a frontier API that
    would 400 on the unknown field)."""
    if "qwen" not in (model or "").lower():
        return False
    host = (endpoint or "").lower()
    return not any(ext in host for ext in _EXTERNAL_LLM_HOSTS)


def apply_enable_thinking(payload: dict, model: str, endpoint: str, thinking: bool) -> dict:
    """Conditionally stamp ``chat_template_kwargs.enable_thinking`` onto a
    chat-completions payload.

    Only mutates the payload when (a) the env kill-switch is on AND (b) the
    target is a local Qwen model — otherwise the field is left off so non-Qwen
    or external/frontier endpoints don't 400 on the unknown key. ``thinking``
    is the caller's intent (False = suppress the <think> trace, the default).
    Idempotent and safe to call on any payload dict."""
    if _disable_thinking_enabled() and _is_local_qwen(model, endpoint):
        payload["chat_template_kwargs"] = {"enable_thinking": bool(thinking)}
    else:
        payload.pop("chat_template_kwargs", None)
    return payload


class LiteLLMProvider:
    def __init__(
        self,
        api_key: str,
        endpoint: str = "",
        model: str = "Qwen3.6-35B-A3B-Vision",
        thinking: bool = False,
    ):
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/") if endpoint else DEFAULT_LITELLM_ENDPOINT
        self.model = model
        # When False (default), suppress the <think>...</think> block that
        # Qwen 3.5+ and some Gemma 4 chat templates emit by default. Flip
        # True for reasoning-heavy tasks where you want the trace.
        self.thinking = thinking

    def _build_payload(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        stream: bool,
        extra_params: Optional[dict] = None,
    ) -> dict:
        # Some LiteLLM upstreams (notably the openai provider for Gemma 4 GGUFs)
        # reject the `reasoning_effort` field outright. We rely on
        # `chat_template_kwargs.enable_thinking=...` to flip reasoning on/off.
        # The field is only injected for a local Qwen target (and only while the
        # MEETING_OPS_DISABLE_THINKING kill-switch is on) — apply_enable_thinking
        # keeps it off external/frontier endpoints that would 400 on it.
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        apply_enable_thinking(payload, self.model, self.endpoint, self.thinking)
        if extra_params:
            # Merge model-tuning knobs (top_p, top_k, presence_penalty,
            # repetition_penalty, etc.) without clobbering the core fields.
            for k, v in extra_params.items():
                if v is not None and k not in payload:
                    payload[k] = v
        return payload

    def _build_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def chat(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 500, temperature: float = 0.7, extra_params: Optional[dict] = None) -> str:
        headers = self._build_headers()
        payload = self._build_payload(system_prompt, user_prompt, max_tokens, temperature, stream=False, extra_params=extra_params)

        # 8192 max_tokens on Qwen 3.6 / P40 can run 3-5 min; the previous
        # 120s default raised ReadTimeout mid-generation and killed the
        # upload pipeline at the summary step. 480s gives generous
        # headroom even for slow models with long outputs.
        try:
            async with httpx.AsyncClient(timeout=480) as client:
                response = await client.post(f"{self.endpoint}/chat/completions", headers=headers, json=payload)
                response.raise_for_status()
            result = response.json()
            msg = result["choices"][0].get("message", {})
            content = (msg.get("content") or "").strip()
            if not content:
                content = (msg.get("reasoning_content") or "").strip()
            if not content:
                raise LLMUnavailable("LLM returned an empty response")
            return content
        except LLMUnavailable:
            raise
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            raise LLMUnavailable(f"LLM request failed: {exc}") from exc

    def chat_sync(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 500, temperature: float = 0.7, extra_params: Optional[dict] = None) -> str:
        """Synchronous chat call. Safe to invoke from sync code or via run_in_executor."""
        headers = self._build_headers()
        payload = self._build_payload(system_prompt, user_prompt, max_tokens, temperature, stream=False, extra_params=extra_params)

        try:
            with httpx.Client(timeout=480) as client:
                response = client.post(f"{self.endpoint}/chat/completions", headers=headers, json=payload)
                if response.status_code == 200:
                    result = response.json()
                    msg = result["choices"][0].get("message", {})
                    content = (msg.get("content") or "").strip()
                    if not content:
                        content = (msg.get("reasoning_content") or "").strip()
                    return content
                else:
                    logger.error(f"LLM API error: {response.status_code} - {response.text[:200]}")
                    return ""
        except Exception as e:
            logger.error(f"LLM sync call failed: {e}")
            return ""

    async def chat_stream(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 500, temperature: float = 0.7) -> AsyncGenerator[str, None]:
        headers = self._build_headers()
        payload = self._build_payload(system_prompt, user_prompt, max_tokens, temperature, stream=True)

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", f"{self.endpoint}/chat/completions", headers=headers, json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data)
                                delta = chunk["choices"][0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
        except httpx.HTTPError as exc:
            raise LLMUnavailable(f"LLM stream failed: {exc}") from exc

    async def health(self) -> dict:
        """Probe upstream availability via /v1/models, falling back to /health.
        Never raises — returns {available, endpoint, model, error}."""
        headers = self._build_headers()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{self.endpoint}/models", headers=headers)
                if resp.status_code == 200:
                    return {"available": True, "endpoint": self.endpoint, "model": self.model, "error": None}
                # Fall back to /health (some llama.cpp builds expose it without auth)
                base = self.endpoint.rsplit("/v1", 1)[0]
                resp2 = await client.get(f"{base}/health")
                if resp2.status_code == 200:
                    return {"available": True, "endpoint": self.endpoint, "model": self.model, "error": None}
                return {
                    "available": False,
                    "endpoint": self.endpoint,
                    "model": self.model,
                    "error": f"HTTP {resp.status_code}",
                }
        except Exception as exc:
            return {"available": False, "endpoint": self.endpoint, "model": self.model, "error": str(exc)}
