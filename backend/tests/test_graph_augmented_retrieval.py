"""Tests for the read-only graph-augmented meeting retriever."""

from __future__ import annotations

import asyncio
import sys
import types
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pytest


if "prometheus_client" not in sys.modules:
    prometheus_client = types.ModuleType("prometheus_client")

    def _make_asgi_app(*args, **kwargs):
        async def _app(scope, receive, send):
            return None

        return _app

    prometheus_client.make_asgi_app = _make_asgi_app
    sys.modules["prometheus_client"] = prometheus_client


def _models():
    from auth.models import Organization
    from database.database import SessionLocal, engine
    from database.models import RecordingSession

    return Organization, SessionLocal, RecordingSession, engine


def _seed_org_with_sessions():
    Organization, SessionLocal, RecordingSession, engine = _models()
    from database.models import Base
    from database.models_rooms import (  # noqa: F401
        ConferenceRoom,
        RoomAcl,
        RoomAudioSource,
        RoomPairingCode,
    )

    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    suffix = uuid.uuid4().hex[:8]
    seed_session_id = f"seed-session-{uuid.uuid4().hex}"
    linked_session_id = f"linked-session-{uuid.uuid4().hex}"

    org = Organization(
        name=f"Graph Augment Org {suffix}",
        slug=f"graph-augment-org-{suffix}",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)

    now = datetime.now(timezone.utc)
    seed = RecordingSession(
        session_id=seed_session_id,
        name="Seed meeting",
        title="Seed meeting",
        status="completed",
        duration=60.0,
        created_at=now,
        started_at=now,
        ended_at=now,
        transcript_simple="Shafen mentioned loan timing and follow-ups.",
        summary="Seed discussion with Shafen.",
        final_summary={
            "executive": "Seed discussion with Shafen.",
            "bullets": ["Loan timing"],
            "decisions": [],
            "action_items": [],
        },
        participants=[],
        tags=[],
        organization_id=org.id,
    )
    linked = RecordingSession(
        session_id=linked_session_id,
        name="Linked meeting",
        title="Legacy25 loans restructure",
        status="completed",
        duration=75.0,
        created_at=now,
        started_at=now,
        ended_at=now,
        transcript_simple="We reviewed loan restructuring options with Shafen.",
        summary="Legacy25 loans restructure with Shafen.",
        final_summary={
            "executive": "Legacy25 loans restructure with Shafen.",
            "bullets": ["Loan restructuring"],
            "decisions": ["Proceed with loan analysis"],
            "action_items": [],
        },
        participants=[],
        tags=[],
        organization_id=org.id,
    )
    db.add_all([seed, linked])
    db.commit()
    db.refresh(seed)
    db.refresh(linked)

    return db, org, seed, linked


@pytest.fixture()
def graph_aug_ctx():
    db, org, seed, linked = _seed_org_with_sessions()
    yield {
        "db": db,
        "org": org,
        "seed": seed,
        "linked": linked,
        "seed_session_id": seed.session_id,
        "linked_session_id": linked.session_id,
    }
    db.close()


def _make_fake_semantic_search(seed_session_id: str):
    class _FakeSemanticSearch:
        def search(self, query: str, limit: int = 5, organization_id: Optional[int] = None):
            return [
                {
                    "session_id": seed_session_id,
                    "title": "Seed meeting",
                    "score": 0.4,
                    "snippet": "Shafen mentioned loan timing and follow-ups.",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "match_type": "semantic",
                }
            ]

        def search_chunks(self, query: str, limit: int = 8, organization_id: Optional[int] = None):
            return [
                {
                    "session_id": seed_session_id,
                    "title": "Seed meeting",
                    "content_type": "transcript",
                    "chunk_index": 0,
                    "text": "Shafen mentioned loan timing and follow-ups.",
                    "speakers": ["Shafen"],
                    "score": 0.91,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]

    return _FakeSemanticSearch


def _make_fake_brigade_client(seed_id: int, linked_id: int):
    class _FakeBrigadeClient:
        is_live = True

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def fetch_entity_context(self, *, entity_name: str, **kwargs) -> Optional[dict[str, Any]]:
            seed_node = f"meeting_ops_meeting_{seed_id}"
            linked_node = f"meeting_ops_meeting_{linked_id}"
            speaker_node = "meeting_ops_speaker_7"

            if entity_name == seed_node:
                return {
                    "entity": {
                        "name": seed_node,
                        "type": "Meeting",
                        "properties": {
                            "id": seed_id,
                            "title": "Seed meeting",
                            "org_id": 1,
                        },
                    },
                    "relationships": [
                        {
                            "from": {"name": seed_node, "type": "Meeting"},
                            "to": {"name": speaker_node, "type": "Speaker"},
                            "relationship": "HAS_SPEAKER",
                        }
                    ],
                    "related_entities": [
                        {
                            "name": speaker_node,
                            "type": "Speaker",
                            "distance": 1,
                            "properties": {
                                "display_name": "Shafen",
                                "org_id": 1,
                            },
                        },
                        {
                            "name": f"meeting_ops_topic_{seed_id}_0",
                            "type": "Topic",
                            "distance": 1,
                            "properties": {
                                "text": "Loan timing and follow-ups",
                                "org_id": 1,
                            },
                        },
                    ],
                }

            if entity_name == speaker_node:
                return {
                    "entity": {
                        "name": speaker_node,
                        "type": "Speaker",
                        "properties": {
                            "display_name": "Shafen",
                            "org_id": 1,
                        },
                    },
                    "relationships": [
                        {
                            "from": {"name": speaker_node, "type": "Speaker"},
                            "to": {"name": linked_node, "type": "Meeting"},
                            "relationship": "HAS_SPEAKER",
                        }
                    ],
                    "related_entities": [
                        {
                            "name": linked_node,
                            "type": "Meeting",
                            "distance": 1,
                            "properties": {
                                "id": linked_id,
                                "title": "Legacy25 loans restructure",
                                "org_id": 1,
                            },
                        }
                    ],
                }

            return None

        async def aclose(self) -> None:
            return None

    return _FakeBrigadeClient


def test_search_meetings_graph_flag_off_is_base_only(monkeypatch, graph_aug_ctx):
    from services import agent_tools

    FakeSemanticSearch = _make_fake_semantic_search(graph_aug_ctx["seed_session_id"])
    monkeypatch.setattr("services.semantic_search_service.SemanticSearchService", FakeSemanticSearch)

    def _fail(*args, **kwargs):
        raise AssertionError("graph augmentation should be disabled")

    monkeypatch.setattr(agent_tools, "augment_meeting_search", _fail)

    result = asyncio.run(
        agent_tools.search_meetings_impl(
            graph_aug_ctx["db"],
            graph_aug_ctx["org"].id,
            query="Shafen loans",
            limit=5,
            graph_augment=False,
        )
    )

    assert result["match_type"] == "semantic"
    assert result["results"][0]["session_id"] == graph_aug_ctx["seed_session_id"]
    assert "graph" not in result


def test_augment_meeting_search_expands_linked_meeting(monkeypatch, graph_aug_ctx):
    from services.graph_augmented_retrieval import augment_meeting_search

    FakeSemanticSearch = _make_fake_semantic_search(graph_aug_ctx["seed_session_id"])
    FakeBrigadeClient = _make_fake_brigade_client(graph_aug_ctx["seed"].id, graph_aug_ctx["linked"].id)
    monkeypatch.setattr("services.graph_augmented_retrieval.SemanticSearchService", FakeSemanticSearch)
    monkeypatch.setattr("services.graph_augmented_retrieval.BrigadeClient", FakeBrigadeClient)

    base_results = [
        {
            "session_id": graph_aug_ctx["seed_session_id"],
            "title": "Seed meeting",
            "score": -1.0,
            "snippet": "Shafen mentioned loan timing and follow-ups.",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "match_type": "semantic",
        }
    ]

    result = asyncio.run(
        augment_meeting_search(
            graph_aug_ctx["db"],
            graph_aug_ctx["org"].id,
            query="what did Shafen and I discuss about loans across meetings?",
            base_results=base_results,
            limit=5,
            enabled=True,
        )
    )

    assert result["enabled"] is True
    assert result["results"][0]["session_id"] == graph_aug_ctx["linked_session_id"]
    assert result["results"][0]["title"] == "Legacy25 loans restructure"
    assert result["results"][0]["graph_bonus"] > 0
    assert "graph_evidence" in result["results"][0]
    assert "Linked meetings" in result["results"][0]["graph_evidence"]["graph_block"]


def test_ask_about_meetings_includes_graph_block(monkeypatch, graph_aug_ctx):
    from services import agent_tools

    FakeSemanticSearch = _make_fake_semantic_search(graph_aug_ctx["seed_session_id"])
    FakeBrigadeClient = _make_fake_brigade_client(graph_aug_ctx["seed"].id, graph_aug_ctx["linked"].id)
    monkeypatch.setattr("services.graph_augmented_retrieval.SemanticSearchService", FakeSemanticSearch)
    monkeypatch.setattr("services.graph_augmented_retrieval.BrigadeClient", FakeBrigadeClient)
    monkeypatch.setattr("services.semantic_search_service.SemanticSearchService", FakeSemanticSearch)

    captured: dict[str, Any] = {}

    class _FakeLLM:
        def chat_sync(self, system_prompt, user_prompt, **kwargs):
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            return "Canned answer"

    result = asyncio.run(
        agent_tools.ask_about_meetings_impl(
            graph_aug_ctx["db"],
            graph_aug_ctx["org"].id,
            _FakeLLM(),
            query="what did Shafen and I discuss about loans across meetings?",
            limit=5,
            graph_augment=True,
        )
    )

    assert result["answer"] == "Canned answer"
    assert "Graph context for" in captured["system_prompt"]
    assert "Legacy25 loans restructure" in captured["system_prompt"]


# ---------------------------------------------------------------------------
# v3.11.x boost-magnitude tuning: direct-text dominance regression coverage.
# ---------------------------------------------------------------------------


def _seed_two_sessions(title_a: str, title_b: str):
    """Seed an org with two completed sessions, returning (db, org, a, b)."""
    Organization, SessionLocal, RecordingSession, engine = _models()
    from database.models import Base
    from database.models_rooms import (  # noqa: F401
        ConferenceRoom,
        RoomAcl,
        RoomAudioSource,
        RoomPairingCode,
    )

    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    suffix = uuid.uuid4().hex[:8]
    org = Organization(
        name=f"Graph Tune Org {suffix}",
        slug=f"graph-tune-org-{suffix}",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)

    now = datetime.now(timezone.utc)

    def _mk(title: str):
        return RecordingSession(
            session_id=f"sess-{uuid.uuid4().hex}",
            name=title,
            title=title,
            status="completed",
            duration=60.0,
            created_at=now,
            started_at=now,
            ended_at=now,
            transcript_simple=f"Discussion notes for {title}.",
            summary=f"Summary for {title}.",
            final_summary={"executive": title, "bullets": [], "decisions": [], "action_items": []},
            participants=[],
            tags=[],
            organization_id=org.id,
        )

    a = _mk(title_a)
    b = _mk(title_b)
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    return db, org, a, b


def _speaker_nodes(pairs):
    # NOTE: speaker nodes carry their display_name in `properties`. We expose
    # them via `related_entities` (NOT relationship endpoints) because
    # _collect_related_nodes() keeps the first-seen copy of a node, and bare
    # relationship endpoints have no properties.
    return [
        {
            "name": name,
            "type": "Speaker",
            "distance": 1,
            "properties": {"display_name": display, "org_id": 1},
        }
        for name, display in pairs
    ]


# ---- Regression: exact-title meeting must out-rank a graph-rich sibling ----

_REGRESSION_QUERY = "automatic summarization manual button transcription"
_REGRESSION_EXACT_TITLE = "automatic summarization manual button transcription"
_REGRESSION_SIBLING_TITLE = "Weekly Engineering Sync"


@pytest.fixture()
def regression_ctx():
    # exact = the meeting whose own title matches the query exactly, reachable
    # only through the graph (shared speaker). sibling = a graph-rich seed.
    db, org, sibling, exact = _seed_two_sessions(
        _REGRESSION_SIBLING_TITLE, _REGRESSION_EXACT_TITLE
    )
    yield {
        "db": db,
        "org": org,
        "exact": exact,
        "sibling": sibling,
        "exact_session_id": exact.session_id,
        "sibling_session_id": sibling.session_id,
    }
    db.close()


def _make_regression_semantic(sibling_session_id: str):
    class _FakeSemanticSearch:
        def search(self, query, limit=5, organization_id=None):
            # Only the sibling is recalled by Qdrant; the exact-title meeting
            # is a recall miss and can only surface via graph expansion.
            return [
                {
                    "session_id": sibling_session_id,
                    "title": _REGRESSION_SIBLING_TITLE,
                    "score": 0.2,
                    "snippet": "general engineering updates",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "match_type": "semantic",
                }
            ]

        def search_chunks(self, query, limit=8, organization_id=None):
            return [
                {
                    "session_id": sibling_session_id,
                    "title": _REGRESSION_SIBLING_TITLE,
                    "content_type": "transcript",
                    "chunk_index": 0,
                    "text": "Shafen walked through the sprint board.",
                    "speakers": ["Shafen"],
                    "score": 0.8,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]

    return _FakeSemanticSearch


def _make_regression_brigade(sibling_id: int, exact_id: int):
    class _FakeBrigadeClient:
        is_live = True

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def fetch_entity_context(self, *, entity_name, **kwargs):
            sibling_node = f"meeting_ops_meeting_{sibling_id}"
            exact_node = f"meeting_ops_meeting_{exact_id}"
            shafen = "meeting_ops_speaker_7"

            if entity_name == sibling_node:
                # Rich seed: 5 speakers (so graph_hits piles up) + a topic whose
                # text overlaps every query term (so related_text maxes out).
                return {
                    "entity": {
                        "name": sibling_node,
                        "type": "Meeting",
                        "properties": {"id": sibling_id, "title": _REGRESSION_SIBLING_TITLE, "org_id": 1},
                    },
                    "relationships": [],
                    "related_entities": _speaker_nodes([
                        (shafen, "Shafen"),
                        ("meeting_ops_speaker_8", "Alice"),
                        ("meeting_ops_speaker_9", "Bob"),
                        ("meeting_ops_speaker_10", "Carol"),
                        ("meeting_ops_speaker_11", "Dave"),
                    ]) + [
                        {
                            "name": f"meeting_ops_topic_{sibling_id}_0",
                            "type": "Topic",
                            "distance": 1,
                            "properties": {
                                "text": "automatic summarization manual button transcription review",
                                "org_id": 1,
                            },
                        },
                    ],
                }

            if entity_name == shafen:
                # Shafen also attended the exact-title meeting -> discovered.
                return {
                    "entity": {"name": shafen, "type": "Speaker",
                               "properties": {"display_name": "Shafen", "org_id": 1}},
                    "relationships": [],
                    "related_entities": [
                        {
                            "name": exact_node,
                            "type": "Meeting",
                            "distance": 1,
                            "properties": {"id": exact_id, "title": _REGRESSION_EXACT_TITLE, "org_id": 1},
                        }
                    ],
                }

            return None

        async def aclose(self):
            return None

    return _FakeBrigadeClient


def test_exact_title_outranks_graph_sibling(monkeypatch, regression_ctx):
    """Session-122 regression.

    A meeting whose own title is an exact match for the query must rank #1
    even when a graph-adjacent sibling accumulates a large graph bonus. Before
    the magnitude re-tuning the sibling's graph bonus (capped at 3.0) could
    override the exact-title meeting; afterwards the title boost (2.5) plus the
    direct-text dominance cap keep the exact-title meeting on top.
    """
    from services.graph_augmented_retrieval import augment_meeting_search

    FakeSemantic = _make_regression_semantic(regression_ctx["sibling_session_id"])
    FakeBrigade = _make_regression_brigade(regression_ctx["sibling"].id, regression_ctx["exact"].id)
    monkeypatch.setattr("services.graph_augmented_retrieval.SemanticSearchService", FakeSemantic)
    monkeypatch.setattr("services.graph_augmented_retrieval.BrigadeClient", FakeBrigade)

    base_results = [
        {
            "session_id": regression_ctx["sibling_session_id"],
            "title": _REGRESSION_SIBLING_TITLE,
            "score": 0.2,
            "snippet": "general engineering updates",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "match_type": "semantic",
        }
    ]

    result = asyncio.run(
        augment_meeting_search(
            regression_ctx["db"],
            regression_ctx["org"].id,
            query=_REGRESSION_QUERY,
            base_results=base_results,
            limit=5,
            enabled=True,
        )
    )

    # Sanity: the graph expansion actually discovered the exact-title meeting.
    discovered = {r["session_id"] for r in result["results"]}
    assert regression_ctx["exact_session_id"] in discovered, (
        "exact-title meeting was not discovered via the graph; fixture wiring broke"
    )

    top = result["results"][0]
    assert top["session_id"] == regression_ctx["exact_session_id"], (
        "exact-title meeting should rank #1; got order "
        + ", ".join(f'{r["title"]}={r["score"]}' for r in result["results"])
    )
    assert top["title"] == _REGRESSION_EXACT_TITLE
    # And it must genuinely beat the sibling, not merely tie.
    sibling_row = next(r for r in result["results"] if r["session_id"] == regression_ctx["sibling_session_id"])
    assert top["score"] > sibling_row["score"]


# ---- No direct-text signal: graph still re-ranks meaningfully -------------

_NO_TEXT_QUERY = "quarterly budget planning"


@pytest.fixture()
def no_text_ctx():
    # Neither title overlaps the query; one meeting is graph-connected, the
    # other is an isolated, unrelated meeting.
    db, org, graphy, unrelated = _seed_two_sessions("Roadmap retro", "Coffee chat")
    yield {
        "db": db,
        "org": org,
        "graphy": graphy,
        "unrelated": unrelated,
        "graphy_session_id": graphy.session_id,
        "unrelated_session_id": unrelated.session_id,
    }
    db.close()


def _make_no_text_semantic(graphy_id: str, unrelated_id: str):
    class _FakeSemanticSearch:
        def search(self, query, limit=5, organization_id=None):
            return [
                {"session_id": graphy_id, "title": "Roadmap retro", "score": 0.3,
                 "snippet": "retro notes", "created_at": datetime.now(timezone.utc).isoformat(),
                 "match_type": "semantic"},
                {"session_id": unrelated_id, "title": "Coffee chat", "score": 0.3,
                 "snippet": "casual chat", "created_at": datetime.now(timezone.utc).isoformat(),
                 "match_type": "semantic"},
            ]

        def search_chunks(self, query, limit=8, organization_id=None):
            return [
                {"session_id": graphy_id, "title": "Roadmap retro", "content_type": "transcript",
                 "chunk_index": 0, "text": "Priya owns the rollout.", "speakers": ["Priya"],
                 "score": 0.7, "created_at": datetime.now(timezone.utc).isoformat()},
            ]

    return _FakeSemanticSearch


def _make_no_text_brigade(graphy_pk: int):
    class _FakeBrigadeClient:
        is_live = True

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def fetch_entity_context(self, *, entity_name, **kwargs):
            graphy_node = f"meeting_ops_meeting_{graphy_pk}"
            if entity_name == graphy_node:
                return {
                    "entity": {"name": graphy_node, "type": "Meeting",
                               "properties": {"id": graphy_pk, "title": "Roadmap retro", "org_id": 1}},
                    "relationships": [],
                    "related_entities": _speaker_nodes([
                        ("meeting_ops_speaker_3", "Priya"),
                        ("meeting_ops_speaker_4", "Sam"),
                        ("meeting_ops_speaker_5", "Lee"),
                    ]),
                }
            # The unrelated meeting has no graph neighborhood at all.
            return None

        async def aclose(self):
            return None

    return _FakeBrigadeClient


def test_graph_signal_still_works_without_direct_text(monkeypatch, no_text_ctx):
    """With zero title overlap, the graph-connected meeting must still out-rank
    an unrelated one (the graph layer keeps re-ranking when titles don't help).
    """
    from services.graph_augmented_retrieval import augment_meeting_search

    FakeSemantic = _make_no_text_semantic(
        no_text_ctx["graphy_session_id"], no_text_ctx["unrelated_session_id"]
    )
    FakeBrigade = _make_no_text_brigade(no_text_ctx["graphy"].id)
    monkeypatch.setattr("services.graph_augmented_retrieval.SemanticSearchService", FakeSemantic)
    monkeypatch.setattr("services.graph_augmented_retrieval.BrigadeClient", FakeBrigade)

    base_results = [
        {"session_id": no_text_ctx["graphy_session_id"], "title": "Roadmap retro", "score": 0.3,
         "snippet": "retro notes", "created_at": datetime.now(timezone.utc).isoformat(),
         "match_type": "semantic"},
        {"session_id": no_text_ctx["unrelated_session_id"], "title": "Coffee chat", "score": 0.3,
         "snippet": "casual chat", "created_at": datetime.now(timezone.utc).isoformat(),
         "match_type": "semantic"},
    ]

    result = asyncio.run(
        augment_meeting_search(
            no_text_ctx["db"],
            no_text_ctx["org"].id,
            query=_NO_TEXT_QUERY,
            base_results=base_results,
            limit=5,
            enabled=True,
        )
    )

    order = [r["session_id"] for r in result["results"]]
    assert order.index(no_text_ctx["graphy_session_id"]) < order.index(no_text_ctx["unrelated_session_id"])
    graphy_row = next(r for r in result["results"] if r["session_id"] == no_text_ctx["graphy_session_id"])
    assert graphy_row["graph_bonus"] > 0


# ---- Magnitude bounds ------------------------------------------------------


def test_graph_bonus_magnitude_bounds():
    from services import graph_augmented_retrieval as gar

    assert gar._MAX_GRAPH_BONUS <= 1.5

    # Worst case: exact title + many graph hits + full related-text overlap.
    worst = gar._meeting_bonus(
        "alpha beta gamma delta",
        "alpha beta gamma delta",
        seed_score=0.0,
        graph_hits=[f"m{i}" for i in range(12)],
        related_text=["alpha beta gamma delta epsilon zeta eta"],
    )
    assert 0.0 <= worst <= 1.5

    samples = [
        ("loans", "loans", [], []),
        ("loans review", "weekly sync", ["a", "b", "c"], ["loans review notes"]),
        ("budget planning", "totally unrelated title", ["x"], ["random text"]),
        ("", "", [], []),
    ]
    for q, t, gh, rt in samples:
        b = gar._meeting_bonus(q, t, seed_score=0.0, graph_hits=gh, related_text=rt)
        assert 0.0 <= b <= 1.5

    # _final_score: a strong title boost collapses the graph bonus to a
    # tiebreaker; a zero title boost lets the full graph bonus through.
    assert gar._final_score(0.0, 2.5, 1.5) == 2.5 + 0.25
    assert gar._final_score(0.0, 0.5, 1.5) == 0.5 + 0.5
    assert gar._final_score(0.0, 0.0, 1.5) == 1.5
