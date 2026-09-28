import httpx
import pytest


@pytest.mark.asyncio
async def test_brigade_client_requests_cascading_entity_delete():
    from services.brigade_client import BrigadeClient

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BrigadeClient(base_url="http://brigade", api_key="secret", _client=http)
        result = await client.delete_entity_subgraph(
            name="meeting_ops_meeting_42",
            agent_id="meeting_ops_org_7",
        )

    assert result.ok is True
    assert captured["method"] == "DELETE"
    assert "cascade=true" in captured["url"]
    assert "agent_id=meeting_ops_org_7" in captured["url"]
