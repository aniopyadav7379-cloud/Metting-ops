from unittest.mock import AsyncMock

import httpx
import pytest
from arq import Retry


def test_transient_classifier_distinguishes_dependency_failures():
    from services.providers.impl_llm import LLMUnavailable
    from services.transient_errors import is_transient_error

    request = httpx.Request("GET", "http://service/health")
    assert is_transient_error(LLMUnavailable("down")) is True
    assert is_transient_error(httpx.ConnectError("down", request=request)) is True
    assert is_transient_error(
        httpx.HTTPStatusError(
            "busy",
            request=request,
            response=httpx.Response(503, request=request),
        )
    ) is True
    assert is_transient_error(ValueError("bad input")) is False


@pytest.mark.asyncio
async def test_final_retry_attempt_persists_dead_letter_marker():
    from services.providers.impl_llm import LLMUnavailable
    from services.transient_errors import retry_transient

    redis = AsyncMock()
    with pytest.raises(Retry):
        await retry_transient(
            {"job_id": "dead-job", "job_try": 5, "redis": redis},
            LLMUnavailable("model reloading"),
        )

    redis.setex.assert_awaited_once()
    key, ttl, payload = redis.setex.await_args.args
    assert key == "meeting-ops:dead-letter:dead-job"
    assert ttl >= 24 * 60 * 60
    assert '"permanently_failed": true' in payload
