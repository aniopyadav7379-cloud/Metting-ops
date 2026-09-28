"""Shared transient failure classification for Arq workers."""
from __future__ import annotations

import json
import logging

import httpx
from arq import Retry

from services.providers.impl_llm import LLMUnavailable

logger = logging.getLogger(__name__)


def is_transient_error(exc: BaseException) -> bool:
    if isinstance(exc, (LLMUnavailable, httpx.TimeoutException, httpx.NetworkError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


async def retry_transient(ctx: dict, exc: BaseException) -> None:
    """Persist a final-attempt marker and raise Arq Retry with backoff."""
    attempt = int(ctx.get("job_try") or 1)
    job_id = str(ctx.get("job_id") or "unknown")
    if attempt >= 5:
        marker = {
            "job_id": job_id,
            "attempt": attempt,
            "error": str(exc)[:1000],
            "permanently_failed": True,
        }
        redis = ctx.get("redis")
        if redis is not None:
            await redis.setex(
                f"meeting-ops:dead-letter:{job_id}",
                30 * 24 * 60 * 60,
                json.dumps(marker),
            )
        logger.error("job_permanently_failed %s", marker)
    defer = min(300, 5 * (2 ** max(0, attempt - 1)))
    raise Retry(defer=defer) from exc
