from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
import sys
import types

from services.semantic_search_service import semantic_search


class _FakeCollectionInfo:
    def __init__(self, points_count: int = 1):
        self.points_count = points_count


class _FakeClient:
    def __init__(self, points_count: int = 1):
        self._points_count = points_count
        self.deleted = []
        self.upserts = []

    def get_collection(self, _name: str):
        return _FakeCollectionInfo(points_count=self._points_count)

    def delete(self, **kwargs):
        self.deleted.append(kwargs)

    def upsert(self, *, collection_name, points):
        self.upserts.append({
            "collection_name": collection_name,
            "points": points,
        })


class _FakeHit:
    def __init__(self, payload: dict, score: float):
        self.payload = payload
        self.score = score


def test_index_session_prefixes_title_into_embedding_text(monkeypatch):
    captured_texts: list[str] = []
    fake_client = _FakeClient()

    def fake_embed(texts):
        captured_texts.extend(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(semantic_search, "ensure_collection", lambda: None)
    monkeypatch.setattr(semantic_search, "_get_client", lambda: fake_client)
    monkeypatch.setattr(semantic_search, "_embed", fake_embed)
    monkeypatch.setattr(semantic_search, "_sparse_embed", lambda texts: None)

    fake_qdrant_models = types.ModuleType("qdrant_client.models")

    class _Filter:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _FieldCondition:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _MatchValue:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _SparseVector:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _PointStruct:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    fake_qdrant_models.Filter = _Filter
    fake_qdrant_models.FieldCondition = _FieldCondition
    fake_qdrant_models.MatchValue = _MatchValue
    fake_qdrant_models.SparseVector = _SparseVector
    fake_qdrant_models.PointStruct = _PointStruct
    monkeypatch.setitem(sys.modules, "qdrant_client", types.ModuleType("qdrant_client"))
    monkeypatch.setitem(sys.modules, "qdrant_client.models", fake_qdrant_models)

    count = semantic_search.index_session(
        session_id="sess-1",
        title="Transcription System Review and GUI Refinement",
        transcript="Body text from transcript",
        summary="Executive summary text",
        created_at="2026-05-26T19:52:48.242361+00:00",
        organization_id=1,
    )

    assert count == 3
    assert captured_texts == [
        "Transcription System Review and GUI Refinement\n\nBody text from transcript",
        "Transcription System Review and GUI Refinement\n\nExecutive summary text",
        "Transcription System Review and GUI Refinement\n\nTranscription System Review and GUI Refinement",
    ]
    assert fake_client.upserts, "expected a Qdrant upsert"


def test_reindex_all_uses_final_summary_fallback(monkeypatch):
    captured_calls: list[dict] = []

    completed = SimpleNamespace(
        id=101,
        session_id="sess-101",
        title="Alpha Session",
        name=None,
        organization_id=1,
        status="completed",
        transcript_simple="alpha transcript",
        transcript=None,
        summary="",
        final_summary={
            "executive": "Alpha executive",
            "bullets": ["Alpha bullet"],
            "decisions": ["Alpha decision"],
            "action_items": ["Alpha task"],
        },
        created_at=datetime(2026, 5, 26, 19, 52, 48, tzinfo=timezone.utc),
    )
    failed = SimpleNamespace(
        id=102,
        session_id="sess-102",
        title="Beta Session",
        name=None,
        organization_id=1,
        status="failed",
        transcript_simple="beta transcript",
        transcript=None,
        summary='{"executive":"Legacy beta summary"}',
        final_summary=None,
        created_at=datetime(2026, 5, 25, 19, 52, 48, tzinfo=timezone.utc),
    )
    ignored = SimpleNamespace(
        id=103,
        session_id="sess-103",
        title="Gamma Session",
        name=None,
        organization_id=1,
        status="processing",
        transcript_simple="gamma transcript",
        transcript=None,
        summary="Gamma summary",
        final_summary=None,
        created_at=datetime(2026, 5, 24, 19, 52, 48, tzinfo=timezone.utc),
    )

    class FakeQuery:
        def __init__(self, sessions):
            self._sessions = sessions

        def filter(self, *_args, **_kwargs):
            return FakeQuery(
                [s for s in self._sessions if s.status in {"completed", "failed"}]
            )

        def all(self):
            return list(self._sessions)

    class FakeDB:
        def __init__(self, sessions):
            self._sessions = sessions

        def query(self, _model):
            return FakeQuery(self._sessions)

    fake_db = FakeDB([completed, failed, ignored])

    def fake_index_session(**kwargs):
        captured_calls.append(kwargs)
        return 1

    monkeypatch.setattr(semantic_search, "index_session", fake_index_session)

    result = semantic_search.reindex_all(fake_db, organization_id=1)

    assert result["sessions_indexed"] == 2
    assert result["sessions_skipped"] == 0
    assert result["total_points"] == 2
    assert len(captured_calls) == 2
    assert captured_calls[0]["title"] == "Alpha Session"
    assert "Alpha executive" in captured_calls[0]["summary"]
    assert "Alpha bullet" in captured_calls[0]["summary"]
    assert captured_calls[1]["title"] == "Beta Session"
    assert "Legacy beta summary" in captured_calls[1]["summary"]


def test_search_boosts_exact_title_match(monkeypatch):
    fake_client = _FakeClient(points_count=2)

    hits = [
        _FakeHit(
            payload={
                "session_id": "sess-1",
                "title": "Transcription System Review and GUI Refinement",
                "content_type": "summary",
                "text": "Exact meeting content",
                "created_at": "2026-05-26T19:52:48.242361+00:00",
            },
            score=0.3,
        ),
        _FakeHit(
            payload={
                "session_id": "sess-2",
                "title": "Completely Unrelated Meeting",
                "content_type": "summary",
                "text": "Unrelated content",
                "created_at": "2026-05-25T19:52:48.242361+00:00",
            },
            score=0.95,
        ),
    ]

    monkeypatch.setattr(semantic_search, "ensure_collection", lambda: None)
    monkeypatch.setattr(semantic_search, "_get_client", lambda: fake_client)
    monkeypatch.setattr(
        semantic_search,
        "_hybrid_query",
        lambda query, limit, organization_id=None: (hits, "hybrid"),
    )

    results = semantic_search.search(
        query="Transcription System Review and GUI Refinement",
        limit=5,
        organization_id=1,
    )

    assert results[0]["session_id"] == "sess-1"
    assert results[0]["score"] > results[1]["score"]
