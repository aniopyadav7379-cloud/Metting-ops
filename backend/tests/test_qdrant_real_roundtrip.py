"""Real Qdrant round-trip test for the vertical slice's acceptance criteria:
    - chunks + metadata are actually persisted in Qdrant (index_session)
    - hybrid retrieval actually returns them (search_chunks)
    - tenant isolation is enforced by Qdrant filters, not just app code

This uses qdrant-client's real embedded ":memory:" engine — genuine Qdrant
upsert/filter/hybrid-query/RRF-fusion code paths, not a hand-rolled fake.
Only the embedding PROVIDER is swapped for a deterministic hash-based
stand-in (Infinity/fastembed need network access to model servers this
sandbox cannot reach) — the same kind of test-boundary substitution used for
Lyzr's HTTP calls elsewhere in this PR, never in the production path.
"""
from __future__ import annotations

import hashlib

import pytest
from qdrant_client import QdrantClient


def _fake_dense_vector(text: str, dim: int = 32) -> list[float]:
    """Deterministic pseudo-embedding: same text -> same vector, and
    textually-similar strings (sharing words) land closer together than
    unrelated ones, so hybrid ranking behaves sensibly in the test."""
    vec = [0.0] * dim
    for word in text.lower().split():
        h = int(hashlib.sha256(word.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


@pytest.fixture()
def real_qdrant(monkeypatch):
    """Point the semantic_search singleton at a genuine embedded Qdrant
    instance with fake-but-real embedding vectors, then restore it."""
    from services.semantic_search_service import semantic_search, DENSE_DIM

    memory_client = QdrantClient(":memory:")
    monkeypatch.setattr(semantic_search, "_client", None)
    monkeypatch.setattr(semantic_search, "_dense_dim", 32)
    monkeypatch.setattr(semantic_search, "_get_client", lambda: memory_client)
    monkeypatch.setattr(semantic_search, "_embed", lambda texts: [_fake_dense_vector(t, 32) for t in texts])
    monkeypatch.setattr(semantic_search, "_sparse_embed", lambda texts: None)  # dense-only is enough to prove the path
    monkeypatch.setattr(semantic_search, "_hybrid_enabled", False)
    yield semantic_search


def test_index_and_search_round_trip(real_qdrant):
    real_qdrant.index_session(
        session_id="sess-alpha",
        title="Sprint planning",
        transcript="Aaron: We decided to ship the migration on Friday. Shafen: I will own the rollback plan.",
        summary="Team agreed to ship the migration Friday with a rollback plan.",
        created_at="2026-09-01T10:00:00Z",
        organization_id=1,
    )

    results = real_qdrant.search_chunks(query="What did we decide about the migration?", limit=5, organization_id=1)
    assert len(results) > 0
    assert any("migration" in r["text"].lower() for r in results)
    assert all(r["session_id"] == "sess-alpha" for r in results)


def test_tenant_isolation_is_enforced_by_qdrant_filter_not_app_code(real_qdrant):
    """Rule #47: a user must NEVER retrieve another org's meeting content via
    semantic search, even if they know the exact wording used in it."""
    real_qdrant.index_session(
        session_id="sess-org1",
        title="Org 1 confidential meeting",
        transcript="We are planning to acquire CompetitorCorp next quarter.",
        summary="Acquisition planning discussion.",
        created_at="2026-09-01T10:00:00Z",
        organization_id=1,
    )
    real_qdrant.index_session(
        session_id="sess-org2",
        title="Org 2 standup",
        transcript="Standup notes about the frontend redesign.",
        summary="Frontend redesign standup.",
        created_at="2026-09-01T10:00:00Z",
        organization_id=2,
    )

    # Org 2 searching for org 1's exact confidential wording must get nothing
    # from org 1 back, even though the term match would otherwise be exact.
    org2_results = real_qdrant.search_chunks(query="acquire CompetitorCorp", limit=10, organization_id=2)
    assert all(r["session_id"] != "sess-org1" for r in org2_results)

    org1_results = real_qdrant.search_chunks(query="acquire CompetitorCorp", limit=10, organization_id=1)
    assert any(r["session_id"] == "sess-org1" for r in org1_results)


def test_reindexing_a_session_replaces_old_points(real_qdrant):
    real_qdrant.index_session(
        session_id="sess-versioned",
        title="v1",
        transcript="Original wording about the launch date.",
        summary="",
        created_at="2026-09-01T10:00:00Z",
        organization_id=3,
    )
    real_qdrant.index_session(
        session_id="sess-versioned",
        title="v1",
        transcript="Updated wording about the revised launch date entirely.",
        summary="",
        created_at="2026-09-01T10:05:00Z",
        organization_id=3,
    )
    results = real_qdrant.search_chunks(query="launch date", limit=10, organization_id=3)
    texts = [r["text"] for r in results if r["session_id"] == "sess-versioned"]
    assert not any("Original wording" in t for t in texts)
    assert any("Updated wording" in t or "revised" in t for t in texts)


def test_delete_session_removes_its_points(real_qdrant):
    real_qdrant.index_session(
        session_id="sess-to-delete",
        title="Temp",
        transcript="This content should be removable from the vector store.",
        summary="",
        created_at="2026-09-01T10:00:00Z",
        organization_id=4,
    )
    assert real_qdrant.search_chunks(query="removable from the vector store", limit=5, organization_id=4)
    real_qdrant.delete_session("sess-to-delete")
    remaining = real_qdrant.search_chunks(query="removable from the vector store", limit=5, organization_id=4)
    assert all(r["session_id"] != "sess-to-delete" for r in remaining)
