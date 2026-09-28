"""Phase 3I - end-to-end product test.

SOURCE 1: Meeting (Architecture Review)
SOURCE 2: Second Meeting (Architecture Follow-up)
SOURCE 3: Lecture (CNN Pooling)
SOURCE 4: Supporting Document (Security Policy)
"""
from __future__ import annotations

import hashlib
import json as json_mod
import uuid

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


def _create_session(transcript: str, title: str, ai_insights: dict = None) -> str:
    from database.database import SessionLocal
    from database.models import RecordingSession, Transcription

    db = SessionLocal()
    try:
        s = RecordingSession(
            session_id=str(uuid.uuid4()), name=title, title=title,
            transcript=transcript, transcript_simple=transcript,
            organization_id=1, status="completed", ai_insights=ai_insights,
        )
        db.add(s)
        db.flush()
        db.add(Transcription(session_id=s.id, text=transcript, speaker="Aaron", start_time=0.0, end_time=10.0))
        db.commit()
        db.refresh(s)
        return s.session_id
    finally:
        db.close()


def test_end_to_end_meetings_lecture_and_document(client, monkeypatch):
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "prov-e2e-p3i")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "cred-e2e-p3i")
    from database.database import SessionLocal
    from database.models import OrgProviderSettings
    from services.providers.crypto import encrypt_api_key

    db = SessionLocal()
    db.query(OrgProviderSettings).filter(OrgProviderSettings.organization_id == 1, OrgProviderSettings.service_kind == "llm").delete()
    db.commit()
    db.add(OrgProviderSettings(organization_id=1, service_kind="llm", provider_name="lyzr", endpoint_url="https://agent-prod.studio.lyzr.ai", api_key_encrypted=encrypt_api_key("k"), model_name="", overrides={}))
    db.commit()
    db.close()

    original_client_post = httpx.Client.post

    def fake_post_sync(self, url, *args, headers=None, json=None, **kwargs):
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-e2e-p3i"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            msg = json["message"].lower()
            if "moments" in msg or "important" in msg:
                return _FakeResponse(200, {"agent_response": json_mod.dumps({"moments": [
                    {"type": "decision", "description": "Team decided to use OAuth2 with PKCE", "quote": "we decided to use OAuth2 with PKCE for authentication"},
                ]})})
            if "authentication" in msg or "oauth" in msg or "pkce" in msg:
                return _FakeResponse(200, {"agent_response": "The team decided to use OAuth2 with PKCE for authentication, and the security policy confirms this is required for all flows."})
            if "pooling" in msg or ("concept" in msg and "lecture" in msg):
                return _FakeResponse(200, {"agent_response": json_mod.dumps({
                    "concepts": ["max pooling", "average pooling"],
                    "definitions": [{"term": "max pooling", "definition": "takes the max value per window"}],
                    "examples": ["comparing pooling strategies"],
                    "questions": ["Does pooling lose information?"],
                    "study_notes": "Pooling reduces feature map size; max pooling keeps the strongest signal.",
                })})
            return _FakeResponse(200, {"agent_response": json["message"][:200]})
        return original_client_post(self, url, *args, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", fake_post_sync)

    async def fake_post_async(self, url, headers=None, json=None):
        return fake_post_sync(None, url, headers=headers, json=json)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_async)

    headers = _admin_bearer(client)

    from services.semantic_search_service import semantic_search

    memory_client = QdrantClient(":memory:")
    monkeypatch.setattr(semantic_search, "_client", None)
    monkeypatch.setattr(semantic_search, "_dense_dim", 32)
    monkeypatch.setattr(semantic_search, "_get_client", lambda: memory_client)
    monkeypatch.setattr(semantic_search, "_embed", lambda texts: [_fake_dense_vector(t, 32) for t in texts])
    monkeypatch.setattr(semantic_search, "_sparse_embed", lambda texts: None)
    monkeypatch.setattr(semantic_search, "_hybrid_enabled", False)

    # SOURCE 1: Meeting
    meet1_id = _create_session(
        "Aaron: We need to decide on the authentication approach. Shafen: we decided to use OAuth2 with PKCE for authentication for the mobile app.",
        "Auth Design Review",
        ai_insights={"summary": "Decided on OAuth2 with PKCE.", "key_decisions": ["Use OAuth2 with PKCE"], "follow_ups": ["Need to confirm token rotation policy"]},
    )
    semantic_search.index_session(session_id=meet1_id, title="Auth Design Review", transcript="We decided to use OAuth2 with PKCE for authentication.", summary="Decided on OAuth2 with PKCE.", created_at="2026-09-01T10:00:00Z", organization_id=1, source_kind="meeting")

    # SOURCE 2: Second meeting
    meet2_id = _create_session(
        "Shafen: Following up, we also need MFA for admin accounts. Aaron: Agreed, adding MFA is a risk mitigation for admin access.",
        "Auth Follow-up",
        ai_insights={"summary": "MFA added for admins.", "key_decisions": ["Add MFA for admin accounts"], "follow_ups": ["MFA vendor not yet selected"]},
    )
    semantic_search.index_session(session_id=meet2_id, title="Auth Follow-up", transcript="We also need MFA for admin accounts as risk mitigation.", summary="MFA added for admins.", created_at="2026-09-02T10:00:00Z", organization_id=1, source_kind="meeting")

    # SOURCE 3: Lecture
    lec_id = _create_session("Professor: Today we cover CNN pooling. Max pooling takes the maximum value per window.", "Deep Learning Lecture 4")
    mark_resp = client.put(f"/api/lectures/{lec_id}/mark", headers=headers, json={"course": "CS 231n", "instructor": "Dr. Lee", "lecture_number": 4})
    assert mark_resp.status_code == 200, mark_resp.text
    assert mark_resp.json()["extraction_status"] == "pending"

    extract_resp = client.post(f"/api/lectures/{lec_id}/extract", headers=headers)
    assert extract_resp.status_code == 200, extract_resp.text
    lecture_body = extract_resp.json()
    assert "max pooling" in lecture_body["concepts"]
    assert lecture_body["study_notes"]

    # SOURCE 4: Supporting document
    doc_resp = client.post(
        "/api/documents/upload", headers=headers,
        files={"file": ("security_policy.txt", b"All authentication flows must use OAuth2 with PKCE per company security policy.", "text/plain")},
    )
    assert doc_resp.status_code == 200, doc_resp.text
    doc_id = doc_resp.json()["doc_id"]
    assert doc_resp.json()["status"] == "indexed"

    results = semantic_search.search_chunks(query="authentication OAuth2 PKCE", limit=20, organization_id=1)
    session_ids_found = {r["session_id"] for r in results}
    assert meet1_id in session_ids_found or meet2_id in session_ids_found
    assert doc_id in session_ids_found
    kinds_found = {r["source_kind"] for r in results}
    assert "document" in kinds_found

    org2_results = semantic_search.search_chunks(query="authentication OAuth2 PKCE", limit=20, organization_id=2)
    assert all(r["session_id"] not in {meet1_id, meet2_id, lec_id, doc_id} for r in org2_results)

    ask_resp = client.post("/api/ai-chat/rag/query", headers=headers, json={"message": "What did we decide about authentication?", "limit": 10})
    assert ask_resp.status_code == 200, ask_resp.text
    ask_body = ask_resp.json()
    assert "oauth2" in ask_body["answer"].lower()
    ask_sources = {s["session_id"] for s in ask_body["sources"]}
    assert doc_id in ask_sources

    decisions_resp = client.get("/api/decisions", headers=headers)
    assert decisions_resp.status_code == 200
    decision_texts = {d["text"] for d in decisions_resp.json()}
    assert "Use OAuth2 with PKCE" in decision_texts
    assert "Add MFA for admin accounts" in decision_texts

    risks_resp = client.get("/api/risks", headers=headers)
    assert risks_resp.status_code == 200
    risk_texts = {r["text"] for r in risks_resp.json()}
    assert "Need to confirm token rotation policy" in risk_texts
    assert "MFA vendor not yet selected" in risk_texts

    moments_resp = client.post(f"/api/moments/{meet1_id}/extract", headers=headers)
    assert moments_resp.status_code == 200, moments_resp.text
    moments = moments_resp.json()
    assert len(moments) == 1
    assert moments[0]["moment_type"] == "decision"
    assert moments[0]["timestamp"] is not None

    lecture_get = client.get(f"/api/lectures/{lec_id}", headers=headers)
    assert lecture_get.status_code == 200
    assert lecture_get.json()["course"] == "CS 231n"

    for source in ask_body["sources"]:
        assert source["session_id"]
        assert "source_kind" in source
    assert moments[0]["speaker"] == "Aaron"
