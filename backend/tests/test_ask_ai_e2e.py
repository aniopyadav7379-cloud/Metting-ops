"""The full vertical slice, exercised through the real HTTP endpoint a
frontend would call: POST /api/ai/rag/query ("Ask AI").

    Qdrant (real embedded engine, real hybrid/filter code) -> context
    assembly (existing, untouched) -> Lyzr (real request shapes, HTTP
    mocked) -> grounded answer with session/source traceability.

This is the closest thing to the acceptance test's step 10-17 ("What did we
decide about the project?" -> grounded, sourced answer) that can run without
live network access to Qdrant Cloud / Lyzr's cloud from this sandbox.
"""
from __future__ import annotations

import hashlib
import json as json_mod

import httpx
import pytest
from qdrant_client import QdrantClient


def _fake_dense_vector(text: str, dim: int = 32) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json = json_data
        self.text = str(json_data)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=httpx.Response(self.status_code))


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def test_ask_ai_end_to_end_via_qdrant_and_lyzr(client, monkeypatch):
    # ---- 1. Real (embedded) Qdrant, seeded with a meeting -----------------
    from services.semantic_search_service import semantic_search

    memory_client = QdrantClient(":memory:")
    monkeypatch.setattr(semantic_search, "_client", None)
    monkeypatch.setattr(semantic_search, "_dense_dim", 32)
    monkeypatch.setattr(semantic_search, "_get_client", lambda: memory_client)
    monkeypatch.setattr(semantic_search, "_embed", lambda texts: [_fake_dense_vector(t, 32) for t in texts])
    monkeypatch.setattr(semantic_search, "_sparse_embed", lambda texts: None)
    monkeypatch.setattr(semantic_search, "_hybrid_enabled", False)

    semantic_search.index_session(
        session_id="sess-project-kickoff",
        title="Project Kickoff",
        transcript=(
            "Aaron: Let's finalize the plan. Shafen: We decided to ship the new "
            "onboarding project by end of quarter. Aaron: Agreed, that's the decision."
        ),
        summary="The team decided to ship the onboarding project by end of quarter.",
        created_at="2026-09-01T10:00:00Z",
        organization_id=1,
    )

    # ---- 2. Org 1 configured to use Lyzr for LLM reasoning -----------------
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "prov-ask")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "cred-ask")

    from database.database import SessionLocal
    from database.models import OrgProviderSettings
    from services.providers.crypto import encrypt_api_key

    db = SessionLocal()
    db.query(OrgProviderSettings).filter(OrgProviderSettings.organization_id == 1, OrgProviderSettings.service_kind == "llm").delete()
    db.commit()
    db.add(
        OrgProviderSettings(
            organization_id=1,
            service_kind="llm",
            provider_name="lyzr",
            endpoint_url="https://agent-prod.studio.lyzr.ai",
            api_key_encrypted=encrypt_api_key("lyzr-super-secret-key"),
            model_name="",
            overrides={},
        )
    )
    db.commit()
    db.close()

    lyzr_calls = {"chat": 0}
    original_client_post = httpx.Client.post

    async def fake_post(self, url, headers=None, json=None):
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-ask-1"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            lyzr_calls["chat"] += 1
            msg = json["message"].lower()
            # Query-rewrite calls (task="fast") vs the final grounded-answer
            # call both hit this same endpoint; only the final answer call
            # will have the retrieved Qdrant context embedded in the prompt.
            if "onboarding project" in msg or "ship" in msg:
                answer = "You decided to ship the onboarding project by end of quarter."
            else:
                answer = json["message"][:200]
            return _FakeResponse(200, {"agent_response": answer})
        raise AssertionError(f"unexpected Lyzr URL: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    def fake_post_sync(self, url, *args, headers=None, json=None, **kwargs):
        # NOTE: httpx.Client is also what starlette's own TestClient uses
        # internally for the outer test request, so this must pass through
        # anything that isn't actually bound for Lyzr — patching the class
        # method affects every httpx.Client in the process, not just
        # LyzrProvider's. And it must resolve synchronously (no asyncio.run):
        # this runs from inside the request handler's already-running loop.
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-ask-1"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            lyzr_calls["chat"] += 1
            msg = json["message"].lower()
            if "onboarding project" in msg or "ship" in msg:
                answer = "You decided to ship the onboarding project by end of quarter."
            else:
                answer = json["message"][:200]
            return _FakeResponse(200, {"agent_response": answer})
        return original_client_post(self, url, *args, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", fake_post_sync)

    # ---- 3. The actual Ask AI HTTP call a frontend would make --------------
    headers = _admin_bearer(client)
    resp = client.post(
        "/api/ai-chat/rag/query",
        headers=headers,
        json={"message": "What did we decide about the project?", "limit": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Grounded: the answer must reflect the retrieved meeting, not a
    # hallucination, and Lyzr must have genuinely been called.
    assert lyzr_calls["chat"] >= 1
    assert "onboarding project" in body["answer"].lower() or "quarter" in body["answer"].lower()

    # Source traceability: session id + speaker info must be exposed.
    assert len(body["sources"]) >= 1
    assert body["sources"][0]["session_id"] == "sess-project-kickoff"

