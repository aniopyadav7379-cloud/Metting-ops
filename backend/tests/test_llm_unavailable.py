import httpx
import pytest


class _FakeClient:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, *args, **kwargs):
        return self.response


@pytest.mark.asyncio
async def test_async_llm_raises_typed_error_on_http_failure(monkeypatch):
    from services.providers import impl_llm

    request = httpx.Request("POST", "http://llm/v1/chat/completions")
    response = httpx.Response(503, request=request, text="reloading")
    monkeypatch.setattr(
        impl_llm.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeClient(response),
    )
    provider = impl_llm.LiteLLMProvider("", endpoint="http://llm/v1")

    with pytest.raises(impl_llm.LLMUnavailable):
        await provider.chat("system", "user")


@pytest.mark.asyncio
async def test_async_llm_raises_typed_error_on_empty_content(monkeypatch):
    from services.providers import impl_llm

    request = httpx.Request("POST", "http://llm/v1/chat/completions")
    response = httpx.Response(
        200,
        request=request,
        json={"choices": [{"message": {"content": ""}}]},
    )
    monkeypatch.setattr(
        impl_llm.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeClient(response),
    )
    provider = impl_llm.LiteLLMProvider("", endpoint="http://llm/v1")

    with pytest.raises(impl_llm.LLMUnavailable):
        await provider.chat("system", "user")
