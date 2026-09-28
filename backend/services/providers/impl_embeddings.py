import logging
import os

import httpx

from .protocols import EmbeddingsProvider

logger = logging.getLogger(__name__)

DEFAULT_INFINITY_ENDPOINT = "http://unicorn-infinity-proxy:8086/v1"


class InfinityProvider:
    def __init__(self, api_key: str = "", endpoint: str = "", model: str = "Qwen/Qwen3-Embedding-0.6B"):
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/") if endpoint else DEFAULT_INFINITY_ENDPOINT
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "input": texts,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.endpoint}/embeddings", headers=headers, json=payload)
            if response.status_code == 200:
                result = response.json()
                return [item["embedding"] for item in result.get("data", [])]
            else:
                logger.error(f"Embedding API error: {response.status_code}")
                return []

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        import requests
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "input": texts,
        }

        try:
            response = requests.post(f"{self.endpoint}/embeddings", headers=headers, json=payload, timeout=60)
            if response.status_code == 200:
                result = response.json()
                return [item["embedding"] for item in result.get("data", [])]
        except Exception as e:
            logger.error(f"Embedding API sync error: {e}")
        return []
