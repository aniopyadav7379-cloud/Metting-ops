"""Tests for api/lectures.py — Lecture as a first-class domain, not a
renamed meeting. Covers marking a session as a lecture, structured
extraction, and (critically) that a lecture is searchable through the
exact same Qdrant + Ask AI pipeline a meeting uses, per Phase 3 section 2's
own example: "Where was CNN pooling explained?"
"""
from __future__ import annotations

import hashlib

import httpx
import pytest
from qdrant_client import QdrantClient


def _admin_bearer(client):
    login = client.post("/api/auth/login", data={"username": "admin", "password": "admin123"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _create_session_with_transcript(client, transcript: str, transcript_simple: str = None) -> str:
    """Create a RecordingSession directly via DB (no full audio pipeline
    needed for these tests) and return its session_id."""
    from database.database import SessionLocal
    from database.models import RecordingSession
    import uuid

    db = SessionLocal()
    try:
        s = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="Intro to Deep Learning - Lecture 4",
            title="Intro to Deep Learning - Lecture 4",
            transcript=transcript,
            transcript_simple=transcript_simple or transcript,
            organization_id=1,
            status="completed",
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.session_id
    finally:
        db.close()


LECTURE_TRANSCRIPT = (
    "Professor: Today we'll cover convolutional neural networks. "
    "Let's start with pooling. Max pooling reduces the spatial dimensions "
    "of a feature map by taking the maximum value in each window. This is "
    "different from average pooling, which takes the mean. Pooling helps "
    "control overfitting and reduces computation. "
    "Student: Does pooling lose information? "
    "Professor: Yes, that's the tradeoff — some spatial detail is lost in "
    "exchange for translation invariance and a smaller parameter count."
)


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


# ---------------------------------------------------------------------------
# Marking / metadata / lifecycle
# ---------------------------------------------------------------------------

def test_mark_session_as_lecture_and_fetch_metadata(client):
    session_id = _create_session_with_transcript(client, LECTURE_TRANSCRIPT)
    headers = _admin_bearer(client)

    mark = client.put(
        f"/api/lectures/{session_id}/mark",
        headers=headers,
        json={"course": "CS 231n", "subject": "Deep Learning", "instructor": "Dr. Lee", "lecture_number": 4},
    )
    assert mark.status_code == 200, mark.text
    body = mark.json()
    assert body["course"] == "CS 231n"
    assert body["instructor"] == "Dr. Lee"
    assert body["lecture_number"] == 4
    assert body["extraction_status"] == "pending"

    fetched = client.get(f"/api/lectures/{session_id}", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["course"] == "CS 231n"


def test_unmarked_session_is_not_a_lecture(client):
    session_id = _create_session_with_transcript(client, "just a regular meeting transcript")
    headers = _admin_bearer(client)
    resp = client.get(f"/api/lectures/{session_id}", headers=headers)
    assert resp.status_code == 404


def test_lecture_list_only_returns_marked_sessions(client):
    lecture_id = _create_session_with_transcript(client, LECTURE_TRANSCRIPT)
    meeting_id = _create_session_with_transcript(client, "ordinary standup notes")
    headers = _admin_bearer(client)
    client.put(f"/api/lectures/{lecture_id}/mark", headers=headers, json={"course": "CS 231n"})

    listing = client.get("/api/lectures", headers=headers)
    assert listing.status_code == 200
    ids = [row["session_id"] for row in listing.json()]
    assert lecture_id in ids
    assert meeting_id not in ids


def test_extract_requires_lecture_marking_first(client):
    session_id = _create_session_with_transcript(client, LECTURE_TRANSCRIPT)
    headers = _admin_bearer(client)
    resp = client.post(f"/api/lectures/{session_id}/extract", headers=headers)
    assert resp.status_code == 400


def test_extract_requires_existing_transcript(client):
    session_id = _create_session_with_transcript(client, "")
    headers = _admin_bearer(client)
    client.put(f"/api/lectures/{session_id}/mark", headers=headers, json={"course": "CS 231n"})
    resp = client.post(f"/api/lectures/{session_id}/extract", headers=headers)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Structured extraction — genuinely lecture-shaped, real Lyzr request/response
# ---------------------------------------------------------------------------

def test_extraction_produces_concepts_definitions_and_study_notes(client, monkeypatch):
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_PROVIDER_ID", "prov-lec")
    monkeypatch.setattr("services.providers.impl_lyzr.LYZR_LLM_CREDENTIAL_ID", "cred-lec")

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
            api_key_encrypted=encrypt_api_key("k"),
            model_name="",
            overrides={},
        )
    )
    db.commit()
    db.close()

    import json as json_mod

    original_client_post = httpx.Client.post

    def fake_post_sync(self, url, *args, headers=None, json=None, **kwargs):
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-lec-1"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            assert "pooling" in json["message"].lower()
            return _FakeResponse(
                200,
                {
                    "agent_response": json_mod.dumps(
                        {
                            "concepts": ["max pooling", "average pooling", "translation invariance"],
                            "definitions": [
                                {"term": "max pooling", "definition": "takes the maximum value in each window"}
                            ],
                            "examples": ["comparing max vs average pooling on a feature map"],
                            "questions": ["Does pooling lose information?"],
                            "study_notes": "Pooling reduces feature-map dimensions; max pooling keeps the strongest activation per window.",
                        }
                    )
                },
            )
        return original_client_post(self, url, *args, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", fake_post_sync)

    session_id = _create_session_with_transcript(client, LECTURE_TRANSCRIPT)
    headers = _admin_bearer(client)
    client.put(f"/api/lectures/{session_id}/mark", headers=headers, json={"course": "CS 231n", "instructor": "Dr. Lee"})

    resp = client.post(f"/api/lectures/{session_id}/extract", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "max pooling" in body["concepts"]
    assert body["definitions"][0]["term"] == "max pooling"
    assert "Does pooling lose information?" in body["questions"]
    assert body["extraction_status"] == "completed"
    assert "Pooling" in body["study_notes"]


def test_extraction_degrades_gracefully_on_llm_failure(client, monkeypatch):
    session_id = _create_session_with_transcript(client, LECTURE_TRANSCRIPT)
    headers = _admin_bearer(client)
    client.put(f"/api/lectures/{session_id}/mark", headers=headers, json={"course": "CS 231n"})

    # No provider configured at all for org 1 -> _resolve_llm returns None
    # -> extraction returns the empty-but-valid shape, never a 500.
    resp = client.post(f"/api/lectures/{session_id}/extract", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["concepts"] == []
    assert body["extraction_status"] == "completed"


# ---------------------------------------------------------------------------
# The explicit Phase 3 example: lecture content is searchable and
# answerable through the SAME Qdrant + Ask AI pipeline as a meeting.
# ---------------------------------------------------------------------------

def _fake_dense_vector(text: str, dim: int = 32) -> list[float]:
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def test_lecture_content_is_searchable_and_answerable_via_ask_ai(client, monkeypatch):
    """'Where was CNN pooling explained?' — Phase 3 section 2's own example.
    Proves a lecture-tagged session flows through the exact same
    index_session -> search_chunks -> Ask AI path a meeting uses, with no
    lecture-specific branching required anywhere in that pipeline."""
    from services.semantic_search_service import semantic_search

    memory_client = QdrantClient(":memory:")
    monkeypatch.setattr(semantic_search, "_client", None)
    monkeypatch.setattr(semantic_search, "_dense_dim", 32)
    monkeypatch.setattr(semantic_search, "_get_client", lambda: memory_client)
    monkeypatch.setattr(semantic_search, "_embed", lambda texts: [_fake_dense_vector(t, 32) for t in texts])
    monkeypatch.setattr(semantic_search, "_sparse_embed", lambda texts: None)
    monkeypatch.setattr(semantic_search, "_hybrid_enabled", False)

    session_id = _create_session_with_transcript(client, LECTURE_TRANSCRIPT)
    headers = _admin_bearer(client)
    client.put(f"/api/lectures/{session_id}/mark", headers=headers, json={"course": "CS 231n", "instructor": "Dr. Lee"})

    semantic_search.index_session(
        session_id=session_id,
        title="Intro to Deep Learning - Lecture 4",
        transcript=LECTURE_TRANSCRIPT,
        summary="Lecture covering CNN pooling: max vs average pooling and their tradeoffs.",
        created_at="2026-09-01T10:00:00Z",
        organization_id=1,
    )

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
            organization_id=1, service_kind="llm", provider_name="lyzr",
            endpoint_url="https://agent-prod.studio.lyzr.ai",
            api_key_encrypted=encrypt_api_key("k"), model_name="", overrides={},
        )
    )
    db.commit()
    db.close()

    original_client_post = httpx.Client.post

    async def fake_post(self, url, headers=None, json=None):
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-ask-lec"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            return _FakeResponse(200, {"agent_response": "Pooling (max vs average) was explained in Lecture 4 of CS 231n."})
        raise AssertionError(f"unexpected Lyzr URL: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    def fake_post_sync(self, url, *args, headers=None, json=None, **kwargs):
        if url == "https://agent-prod.studio.lyzr.ai/v3/agents/":
            return _FakeResponse(200, {"id": "agent-ask-lec"})
        if url == "https://agent-prod.studio.lyzr.ai/v3/inference/chat/":
            return _FakeResponse(200, {"agent_response": "Pooling (max vs average) was explained in Lecture 4 of CS 231n."})
        return original_client_post(self, url, *args, headers=headers, json=json, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", fake_post_sync)

    resp = client.post(
        "/api/ai-chat/rag/query",
        headers=headers,
        json={"message": "Where was CNN pooling explained?", "limit": 5},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "pooling" in body["answer"].lower()
    assert len(body["sources"]) >= 1
    assert body["sources"][0]["session_id"] == session_id
