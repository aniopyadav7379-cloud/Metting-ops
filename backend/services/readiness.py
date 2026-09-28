"""Deep dependency probes used by readiness and status endpoints."""
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import text

PROBE_TIMEOUT_SECONDS = float(os.getenv("READINESS_TIMEOUT_SECONDS", "4"))


async def _probe_database() -> dict[str, Any]:
    from database.database import SessionLocal

    def check() -> None:
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()

    await asyncio.to_thread(check)
    return {"ok": True}


async def _probe_redis() -> dict[str, Any]:
    import redis.asyncio as redis

    client = redis.from_url(os.getenv("REDIS_URL", "redis://unicorn-redis:6379/4"))
    try:
        if not await client.ping():
            raise RuntimeError("PING returned false")
    finally:
        await client.aclose()
    return {"ok": True}


async def _probe_qdrant() -> dict[str, Any]:
    from services.semantic_search_service import semantic_search

    await asyncio.to_thread(semantic_search._get_client().get_collections)
    return {"ok": True}


async def _http_health(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
        response.raise_for_status()
    return {"ok": True, "url": url}


async def _probe_llm() -> dict[str, Any]:
    from services.providers.registry import ProviderRegistry

    result = await ProviderRegistry(None).default_llm().health()
    if not result.get("available"):
        raise RuntimeError(result.get("error") or "LLM unavailable")
    return {"ok": True, "model": result.get("model")}


async def _probe_stt() -> dict[str, Any]:
    endpoint = os.getenv("PARAKEET_SERVER_URL", "http://meet-parakeet-svc:8881").rstrip("/")
    return await _http_health(f"{endpoint}/health")


async def _probe_diarization() -> dict[str, Any]:
    endpoint = os.getenv("SPEAKER_SVC_URL", "http://meet-speaker-svc:8889").rstrip("/")
    return await _http_health(f"{endpoint}/health")


async def _probe_tts() -> dict[str, Any]:
    endpoint = os.getenv("KOKORO_ENDPOINT", "http://unicorn-kokoro:8880").rstrip("/")
    # Gateway-routed TTS (KOKORO_ENDPOINT -> the LiteLLM gateway): /health 401s
    # unauthenticated, but an authenticated /v1/models returns 200. Try that
    # first; fall back to the unauthenticated /health a direct Kokoro exposes.
    api_key = (
        os.getenv("KOKORO_API_KEY")
        or os.getenv("LITELLM_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )
    if api_key:
        base = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
        models_url = f"{base}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    models_url, headers={"Authorization": f"Bearer {api_key}"}
                )
                if resp.status_code == 200:
                    return {"ok": True, "url": models_url}
        except Exception:  # noqa: BLE001 - fall back to /health below
            pass
    return await _http_health(f"{endpoint}/health")


async def _probe_infinity() -> dict[str, Any]:
    endpoint = (
        os.getenv("INFINITY_EMBEDDING_ENDPOINT")
        or os.getenv("INFINITY_ENDPOINT")
        or "http://unicorn-infinity-proxy:8086/v1"
    ).rstrip("/")
    # When embeddings route through the LiteLLM gateway (prod), the gateway's
    # /health 401s unauthenticated (and hangs when authed), but an authenticated
    # /models returns 200. Try that first; fall back to the unauthenticated
    # /health that a direct Infinity server exposes (bigboy).
    api_key = (
        os.getenv("INFINITY_API_KEY")
        or os.getenv("LITELLM_API_KEY")
        or os.getenv("OPENAI_API_KEY", "")
    )
    if api_key:
        models_url = f"{endpoint}/models"
        try:
            async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    models_url, headers={"Authorization": f"Bearer {api_key}"}
                )
                if resp.status_code == 200:
                    return {"ok": True, "url": models_url}
        except Exception:  # noqa: BLE001 - fall back to /health below
            pass
    base = endpoint[:-3] if endpoint.endswith("/v1") else endpoint
    return await _http_health(f"{base}/health")


_PROBES: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
    "database": _probe_database,
    "redis": _probe_redis,
    "qdrant": _probe_qdrant,
    "llm": _probe_llm,
    "stt": _probe_stt,
    "diarization": _probe_diarization,
    "tts": _probe_tts,
    "infinity": _probe_infinity,
}


async def _run_probe(name: str, probe: Callable[[], Awaitable[dict[str, Any]]]) -> tuple[str, dict[str, Any]]:
    try:
        result = await asyncio.wait_for(probe(), timeout=PROBE_TIMEOUT_SECONDS + 1)
        return name, {"status": "ok", **result}
    except Exception as exc:  # noqa: BLE001
        return name, {"status": "down", "ok": False, "error": str(exc)[:500]}


async def run_readiness_checks() -> dict[str, Any]:
    results = await asyncio.gather(*(_run_probe(name, probe) for name, probe in _PROBES.items()))
    dependencies = dict(results)
    ready = all(item.get("ok") is True for item in dependencies.values())
    return {"status": "ready" if ready else "degraded", "ready": ready, "dependencies": dependencies}
