"""Phase 3C (cross-source knowledge) and 3D (Ask AI quality) tests.

3C: the system must reason across Meeting + Meeting + Lecture + Document
through the SAME Qdrant collection and the SAME Ask AI endpoint — no
separate vector store, no separate pipeline for documents (verified by
using the exact same `semantic_search` singleton and `/api/ai-chat/rag/query`
endpoint every other test in this suite uses).

3D: answers must expose source_kind (meeting/lecture/document) and must
NOT invent an answer when there's no supporting evidence.
"""
from __future__ import annotations

import hashlib

import httpx
import pytest
from qdrant_client import QdrantClient


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _fake_dense_vector(text: str, dim: int = 32) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


@pytest.fixture()
def real_qdrant(monkeypatch):
    from services.semantic_search_service import semantic_search

    memory_client = QdrantClient(":memory:")
    monkeypatch.setattr(semantic_search, "_client", None)
    monkeypatch.setattr(semantic_search, "_dense_dim", 32)
    monkeypatch.setattr(semantic_search, "_get_client", lambda: memory_client)
    monkeypatch.setattr(semantic_search, "_embed", lambda texts: [_fake_dense_vector(t, 32) for t in texts])
    monkeypatch.setattr(semantic_search, "_sparse_embed", lambda texts: None)
    monkeypatch.setattr(semantic_search, "_hybrid_enabled", False)
    yield semantic_search


class _FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data
        self.text = str(json_data)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=httpx.Response(self.status_code))


def _configure_lyzr(monkeypatch, answer_fn):
    """answer_fn(message: str) -> str, called for every Lyzr chat request
    (both the query-rewrite call and the final answer call)."""
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "prov-x")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "cred-x")

    from database.database import SessionLocal
    from database.models import OrgProviderSettings
    from services.providers.crypto import encrypt_api_key

    db = SessionLocal()
    db.query(OrgProviderSettings).filter(OrgProviderSettings.organization_id == 1, OrgProviderSettings.service_kind == "llm").delete()
    db.commit()
    db.add(
        OrgProviderSettings(
            organization_id=1, service_kind="llm", provider_name="lyzr",
            endpoint_url="https://agent-prod.studio.lyzr.ai",
            api_key_encrypted=encrypt_api_key("k"), model_name="", overrides={},
        )
    )
    db.commit()
    db.close()

    original_client_post = httpx.Client.post

    def fake_post_sync(self, url, *args, headers=None, json=None, **kwargs):
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-x"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            return _FakeResponse(200, {"agent_response": answer_fn(json["message"])})
        return original_client_post(self, url, *args, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", fake_post_sync)

    async def fake_post_async(self, url, headers=None, json=None):
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-x"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            return _FakeResponse(200, {"agent_response": answer_fn(json["message"])})
        raise AssertionError(f"unexpected async URL: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_async)


def _seed_meeting(session_id, title, transcript, summary, source_kind="meeting"):
    from services.semantic_search_service import semantic_search

    semantic_search.index_session(
        session_id=session_id, title=title, transcript=transcript, summary=summary,
        created_at="2026-09-01T10:00:00Z", organization_id=1, source_kind=source_kind,
    )


# ---------------------------------------------------------------------------
# 3C: cross-source retrieval — 2 meetings + 1 lecture + 1 document
# ---------------------------------------------------------------------------

def test_cross_source_retrieval_spans_meetings_lecture_and_document(client, real_qdrant):
    _seed_meeting(
        "meet-auth-1", "Auth Design Review",
        "Aaron: We decided to use OAuth2 with PKCE for the mobile app authentication flow.",
        "Team decided on OAuth2 with PKCE for mobile authentication.",
    )
    _seed_meeting(
        "meet-auth-2", "Auth Follow-up",
        "Shafen: Following up on authentication, we also decided to add MFA for admin accounts.",
        "Follow-up: MFA added for admin accounts.",
    )
    _seed_meeting(
        "lec-cnn-1", "Deep Learning Lecture 4",
        "Professor: Today we cover CNN pooling. Max pooling takes the maximum value in each window.",
        "Lecture on CNN pooling operations.",
        source_kind="lecture",
    )
    _seed_meeting(
        "doc-auth-policy", "Authentication Security Policy",
        "All authentication flows must use OAuth2 with PKCE. Passwords alone are not sufficient for admin access.",
        None,
        source_kind="document",
    )

    results = real_qdrant.search_chunks(query="What did we decide about authentication?", limit=10, organization_id=1)
    session_ids = {r["session_id"] for r in results}
    assert "meet-auth-1" in session_ids or "meet-auth-2" in session_ids
    assert "doc-auth-policy" in session_ids
    kinds_present = {r["source_kind"] for r in results}
    assert "document" in kinds_present


def test_ask_ai_reasons_across_meeting_and_document_together(client, real_qdrant, monkeypatch):
    _seed_meeting(
        "meet-deploy-1", "Deployment Planning",
        "Aaron: We decided to deploy using blue-green deployments starting next sprint.",
        "Decision: adopt blue-green deployments.",
    )
    _seed_meeting(
        "doc-deploy-policy", "Deployment Runbook",
        "The deployment runbook requires a rollback plan and a staging smoke test before every production deploy.",
        None,
        source_kind="document",
    )

    def answer_fn(message: str) -> str:
        if "blue-green" in message.lower() or "rollback" in message.lower():
            return "The team decided to use blue-green deployments, and the runbook requires a rollback plan and staging smoke test before each deploy."
        return message[:100]

    _configure_lyzr(monkeypatch, answer_fn)

    headers = _admin_bearer(client)
    resp = client.post(
        "/api/ai-chat/rag/query",
        headers=headers,
        json={"message": "Which document supports the decision made in the meeting about deployment?", "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "blue-green" in body["answer"].lower()
    assert "rollback" in body["answer"].lower()
    source_ids = {s["session_id"] for s in body["sources"]}
    kinds = {s["source_kind"] for s in body["sources"]}
    assert "meet-deploy-1" in source_ids
    assert "doc-deploy-policy" in source_ids
    assert "document" in kinds
    assert "meeting" in kinds


def test_lecture_and_meeting_are_distinguishable_in_sources(client, real_qdrant, monkeypatch):
    _seed_meeting(
        "meet-qdrant-1", "Vector DB Discussion",
        "We previously discussed Qdrant as our vector database choice for semantic search.",
        "Chose Qdrant for vector search.",
    )
    _seed_meeting(
        "lec-cnn-2", "Deep Learning Lecture 5",
        "Professor explained CNN pooling in detail: max pooling versus average pooling tradeoffs.",
        "CNN pooling lecture.",
        source_kind="lecture",
    )

    def answer_fn(message: str) -> str:
        if "qdrant" in message.lower():
            return "We previously discussed Qdrant as the vector database choice."
        return message[:100]

    _configure_lyzr(monkeypatch, answer_fn)
    headers = _admin_bearer(client)
    resp = client.post("/api/ai-chat/rag/query", headers=headers, json={"message": "What did we previously discuss about Qdrant?", "limit": 10})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    matching = [s for s in body["sources"] if s["session_id"] == "meet-qdrant-1"]
    assert matching and matching[0]["source_kind"] == "meeting"


# ---------------------------------------------------------------------------
# 3D: insufficient evidence — must not hallucinate
# ---------------------------------------------------------------------------

def test_ask_ai_reports_no_results_when_knowledge_base_is_empty(client, real_qdrant):
    """Nothing indexed at all for this org -> the existing no-chunks path
    in api/ai_chat.py must fire, not a hallucinated answer."""
    headers = _admin_bearer(client)
    resp = client.post(
        "/api/ai-chat/rag/query",
        headers=headers,
        json={"message": "What did we decide about the quantum teleportation budget?", "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sources"] == []
    assert body["metadata"].get("chunks_found") == 0
    assert "no indexed" in body["answer"].lower()


def test_ask_ai_does_not_invent_facts_absent_from_context(client, real_qdrant, monkeypatch):
    """Indexed content exists, but for a totally unrelated topic. This
    verifies that when the model follows the insufficient-evidence
    instruction, the app surfaces that faithfully rather than post-
    processing it into a fabricated answer."""
    _seed_meeting(
        "meet-standup-1", "Daily Standup",
        "Aaron: Yesterday I fixed the login bug. Shafen: I'm still working on the CSV export feature.",
        "Standup notes.",
    )

    def answer_fn(message: str) -> str:
        return "I couldn't find enough evidence in the available meetings/lectures to answer that."

    _configure_lyzr(monkeypatch, answer_fn)
    headers = _admin_bearer(client)
    resp = client.post(
        "/api/ai-chat/rag/query",
        headers=headers,
        json={"message": "What was decided about the company's acquisition of a competitor?", "limit": 10},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "couldn't find enough evidence" in body["answer"].lower()
