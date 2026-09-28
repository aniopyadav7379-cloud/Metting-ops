import logging
import os

import httpx

from .protocols import RerankingProvider

logger = logging.getLogger(__name__)

DEFAULT_INFINITY_RERANK_ENDPOINT = "http://unicorn-infinity-proxy:8086/v1"


class InfinityRerankingProvider:
    def __init__(self, api_key: str = "", endpoint: str = "", model: str = "Qwen/Qwen3-Reranker-0.6B"):
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/") if endpoint else DEFAULT_INFINITY_RERANK_ENDPOINT
        self.model = model

    async def rerank(self, query: str, documents: list[str], top_n: int = 5) -> list[dict]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self.endpoint}/rerank", headers=headers, json=payload)
            if response.status_code == 200:
                result = response.json()
                return result.get("results", [])
            else:
                logger.error(f"Reranking API error: {response.status_code}")
                return []
