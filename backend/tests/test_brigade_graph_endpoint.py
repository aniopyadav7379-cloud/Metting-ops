"""Tests for the Brigade Phase 2 read endpoint.

Covers GET /api/sessions/{session_id}/brigade-graph per
docs/brigade-integration-design.md Phase 2:

  1. unauth_returns_401             - no auth header gets 401/403/4xx.
  2. unsynced_session_returns_empty - brigade_graph_node_id NULL ->
                                      {nodes: [], links: [],
                                       reason: 'not_synced_yet'}.
  3. cross_org_isolation_404        - user A cannot read user B's
                                      session's graph (404 — no
                                      existence leak).
  4. synced_session_shapes_response - mocked Brigade client returns a
                                      context payload; endpoint maps
                                      to {nodes, links, graph_url}.
  5. brigade_unreachable_returns_live_failed - when fetch_entity_context
                                      returns None (Brigade down), the
                                      endpoint returns reason='live_failed'
                                      not a 500.
  6. response_cached_30s            - second call within 30s reuses
                                      the cached payload (one Brigade
                                      fetch_entity_context call across
                                      two endpoint hits).
  7. cache_invalidates_on_resync    - bumping brigade_synced_at on the
                                      session row invalidates the cache
                                      (cache key includes synced_at).

All tests mock services.brigade_client.BrigadeClient — no real network.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession

    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _seed_user(db, username: str, password: str, email: str):
    _, User, _, _, _ = _models()
    user = User(
        email=email,
        username=username,
        hashed_password=get_password_hash(password),
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, f"Login failed: {response.text}"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_session(
    db,
    *,
    organization_id: int,
    user_id: int,
    title: str,
    brigade_graph_node_id: Optional[str] = None,
    brigade_synced_at: Optional[datetime] = None,
):
    _, _, _, _, RecordingSession = _models()
    sess = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        status="completed",
        organization_id=organization_id,
        user_id=user_id,
        transcript_simple="hello brigade graph",
        transcript="hello brigade graph",
        duration=120.0,
        participants=[],
        brigade_graph_node_id=brigade_graph_node_id,
        brigade_synced_at=brigade_synced_at,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


@pytest.fixture(autouse=True)
def _clear_brigade_cache():
    """Each test starts with an empty Brigade graph cache. Without
    this, response_cached_30s would interact with other tests."""
    from api.recording import _brigade_graph_cache

    _brigade_graph_cache.clear()
    yield
    _brigade_graph_cache.clear()


@pytest.fixture(autouse=True)
def _scrub_brigade_env(monkeypatch):
    """Tests must not hit real Brigade. Strip env so build_brigade_graph_url
    sees the default and BrigadeClient stays in noop mode unless a test
    monkeypatches it explicitly."""
    monkeypatch.delenv("BRIGADE_API_KEY", raising=False)
    monkeypatch.delenv("BRIGADE_ADMIN_KEY", raising=False)
    monkeypatch.delenv("BRIGADE_TENANCY_MODE", raising=False)
    monkeypatch.delenv("BRIGADE_API_BASE_URL", raising=False)
    yield


@pytest.fixture()
def two_orgs(client):
    """Standard two-org fixture (Alpha / Bravo) with one user each.

    Returns ctx with synced session in org_a, unsynced session in org_a,
    and a session in org_b for cross-org isolation tests.
    """
    Organization, _, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        org_a = Organization(
            name=f"Org Alpha {suffix}", slug=f"alpha-{suffix}", is_active=True
        )
        org_b = Organization(
            name=f"Org Bravo {suffix}", slug=f"bravo-{suffix}", is_active=True
        )
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        caller = _seed_user(
            db, f"caller_{suffix}", "Password123", f"caller_{suffix}@example.com"
        )
        outsider = _seed_user(
            db, f"out_{suffix}", "Password123", f"out_{suffix}@example.com"
        )
        db.add_all([
            UserOrganization(
                user_id=caller.id, organization_id=org_a.id, role="admin"
            ),
            UserOrganization(
                user_id=outsider.id, organization_id=org_b.id, role="user"
            ),
        ])
        db.commit()

        sess_synced = _make_session(
            db,
            organization_id=org_a.id,
            user_id=caller.id,
            title="Synced session",
            brigade_graph_node_id="meeting_ops_meeting_alpha",
            brigade_synced_at=datetime.now(timezone.utc),
        )
        sess_unsynced = _make_session(
            db,
            organization_id=org_a.id,
            user_id=caller.id,
            title="Unsynced session",
        )
        sess_b = _make_session(
            db,
            organization_id=org_b.id,
            user_id=outsider.id,
            title="Bravo session",
            brigade_graph_node_id="meeting_ops_meeting_bravo",
            brigade_synced_at=datetime.now(timezone.utc),
        )

        ctx = {
            "org_a_slug": org_a.slug,
            "org_b_slug": org_b.slug,
            "sess_synced_pub": sess_synced.session_id,
            "sess_synced_pk": sess_synced.id,
            "sess_unsynced_pub": sess_unsynced.session_id,
            "sess_b_pub": sess_b.session_id,
            "caller_username": caller.username,
            "outsider_username": outsider.username,
        }
    finally:
        db.close()

    ctx["headers"] = _login(client, ctx["caller_username"], "Password123")
    ctx["headers"]["X-MeetingOps-Org"] = ctx["org_a_slug"]
    ctx["headers_outsider"] = _login(client, ctx["outsider_username"], "Password123")
    ctx["headers_outsider"]["X-MeetingOps-Org"] = ctx["org_b_slug"]
    return ctx


# ---------------------------------------------------------------------------
# 1. Auth required
# ---------------------------------------------------------------------------


def test_unauth_returns_401(client, two_orgs):
    """No Authorization header -> 4xx (not 200)."""
    ctx = two_orgs
    resp = client.get(
        f"/api/sessions/{ctx['sess_synced_pub']}/brigade-graph",
    )
    # Either 401 (no token) or 403 (forbidden) is acceptable; both
    # prove auth is enforced. The key requirement is "not 200".
    assert resp.status_code in {401, 403}, resp.text


# ---------------------------------------------------------------------------
# 2. Unsynced session returns empty with reason='not_synced_yet'
# ---------------------------------------------------------------------------


def test_unsynced_session_returns_empty(client, two_orgs):
    """A session that's never been written to Brigade returns the
    empty payload + 'not_synced_yet'. The frontend uses this to render
    the empty-state without firing a Brigade lookup."""
    ctx = two_orgs
    resp = client.get(
        f"/api/sessions/{ctx['sess_unsynced_pub']}/brigade-graph",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"] == []
    assert body["links"] == []
    assert body["reason"] == "not_synced_yet"
    assert body["graph_url"] is None
    assert body["focus"] is None


# ---------------------------------------------------------------------------
# 3. Cross-org isolation
# ---------------------------------------------------------------------------


def test_cross_org_isolation_returns_404(client, two_orgs):
    """User A (in org_a) cannot read user B's session graph (in org_b).
    Must be 404, not 403 — we don't leak existence."""
    ctx = two_orgs
    resp = client.get(
        f"/api/sessions/{ctx['sess_b_pub']}/brigade-graph",
        headers=ctx["headers"],
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 4. Synced session returns properly-shaped {nodes, links}
# ---------------------------------------------------------------------------


def test_synced_session_shapes_response(client, two_orgs, monkeypatch):
    """When the session is synced, the endpoint calls Brigade's
    /knowledge/context/{name}, transforms the response into the
    react-force-graph-3d {nodes, links} shape, and returns it
    alongside the deep-link graph_url."""
    ctx = two_orgs

    # Build a fake Brigade context response covering the meeting + 1
    # speaker + 1 action item + 1 topic. The endpoint should map each
    # to a distinct node and create 3 edges.
    fake_context = {
        "success": True,
        "entity": {
            "name": "meeting_ops_meeting_alpha",
            "type": "Meeting",
            "created_at": "2026-05-22T10:00:00",
            "created_by": "meeting_ops_canonical",
            "graph": "agent_meeting_ops_canonical",
        },
        "relationships": [
            {
                "from": {"name": "meeting_ops_meeting_alpha", "type": "Meeting"},
                "to": {"name": "meeting_ops_speaker_1", "type": "Speaker"},
                "relationship": "HAS_SPEAKER",
                "confidence": None,
                "learned_at": "2026-05-22T10:01:00",
                "source_agent": None,
                "graph": "agent_meeting_ops_canonical",
            },
            {
                "from": {"name": "meeting_ops_meeting_alpha", "type": "Meeting"},
                "to": {"name": "meeting_ops_action_1", "type": "ActionItem"},
                "relationship": "HAS_ACTION_ITEM",
                "confidence": None,
                "learned_at": "2026-05-22T10:02:00",
                "source_agent": None,
                "graph": "agent_meeting_ops_canonical",
            },
            {
                "from": {"name": "meeting_ops_meeting_alpha", "type": "Meeting"},
                "to": {"name": "meeting_ops_topic_alpha_0", "type": "Topic"},
                "relationship": "HAS_TOPIC",
                "confidence": None,
                "learned_at": "2026-05-22T10:03:00",
                "source_agent": None,
                "graph": "agent_meeting_ops_canonical",
            },
        ],
        "related_entities": [
            {
                "name": "meeting_ops_speaker_1",
                "type": "Speaker",
                "distance": 1,
                "graph": "agent_meeting_ops_canonical",
            },
            {
                "name": "meeting_ops_action_1",
                "type": "ActionItem",
                "distance": 1,
                "graph": "agent_meeting_ops_canonical",
            },
            {
                "name": "meeting_ops_topic_alpha_0",
                "type": "Topic",
                "distance": 1,
                "graph": "agent_meeting_ops_canonical",
            },
        ],
    }

    # Patch BrigadeClient.fetch_entity_context at module level so the
    # endpoint's `BrigadeClient()` instance picks up the stub.
    import services.brigade_client as bc_mod

    class _StubBrigadeClient:
        is_live = True

        def __init__(self, *args, **kwargs):
            pass

        async def fetch_entity_context(
            self,
            *,
            entity_name: str,
            include_relationships: bool = True,
            include_related: bool = True,
            max_depth: int = 1,
        ) -> Optional[dict[str, Any]]:
            assert entity_name == "meeting_ops_meeting_alpha"
            return fake_context

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(bc_mod, "BrigadeClient", _StubBrigadeClient)

    resp = client.get(
        f"/api/sessions/{ctx['sess_synced_pub']}/brigade-graph",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 4 distinct nodes (meeting + speaker + action + topic).
    assert {n["id"] for n in body["nodes"]} == {
        "meeting_ops_meeting_alpha",
        "meeting_ops_speaker_1",
        "meeting_ops_action_1",
        "meeting_ops_topic_alpha_0",
    }
    # Meeting is the focus node.
    focus_nodes = [n for n in body["nodes"] if n["is_focus"]]
    assert len(focus_nodes) == 1
    assert focus_nodes[0]["id"] == "meeting_ops_meeting_alpha"
    # Labels carry the conceptual entity type.
    labels = {n["id"]: n["label"] for n in body["nodes"]}
    assert labels["meeting_ops_meeting_alpha"] == "Meeting"
    assert labels["meeting_ops_speaker_1"] == "Speaker"
    assert labels["meeting_ops_action_1"] == "ActionItem"
    assert labels["meeting_ops_topic_alpha_0"] == "Topic"
    # 3 edges with relationship types preserved.
    assert len(body["links"]) == 3
    edge_types = {link["type"] for link in body["links"]}
    assert edge_types == {"HAS_SPEAKER", "HAS_ACTION_ITEM", "HAS_TOPIC"}
    # focus + reason fields surface correctly.
    assert body["focus"] == "meeting_ops_meeting_alpha"
    assert body["reason"] is None
    # graph_url is intentionally None — recording.py hardcodes it so the
    # frontend renders the in-app 3D graph from the nodes/links payload
    # instead of deep-linking to Brigade's standalone viewer.
    assert body["graph_url"] is None
    # The graph payload the frontend renders from is still fully present.
    assert "nodes" in body and body["nodes"]
    assert "links" in body and body["links"]
    assert "focus" in body
    assert all("label" in n for n in body["nodes"])


# ---------------------------------------------------------------------------
# 5. Brigade unreachable -> live_failed (not 500)
# ---------------------------------------------------------------------------


def test_brigade_unreachable_returns_live_failed(client, two_orgs, monkeypatch):
    """If Brigade is down OR the entity is missing, the endpoint
    returns 200 with reason='live_failed' so the frontend can render
    the retry empty-state. Never a 500."""
    ctx = two_orgs

    import services.brigade_client as bc_mod

    class _DownBrigadeClient:
        is_live = True

        def __init__(self, *args, **kwargs):
            pass

        async def fetch_entity_context(self, **kwargs) -> Optional[dict[str, Any]]:
            return None  # client swallows errors as None

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(bc_mod, "BrigadeClient", _DownBrigadeClient)

    resp = client.get(
        f"/api/sessions/{ctx['sess_synced_pub']}/brigade-graph",
        headers=ctx["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"] == []
    assert body["links"] == []
    assert body["reason"] == "live_failed"
    # focus + graph_url still surface so the frontend can offer the
    # "open in Brigade" fallback even while inline live is down.
    assert body["focus"] == "meeting_ops_meeting_alpha"


# ---------------------------------------------------------------------------
# 6. 30-second response cache
# ---------------------------------------------------------------------------


def test_response_cached_30s(client, two_orgs, monkeypatch):
    """Second call within 30s reuses the cached payload — Brigade's
    fetch_entity_context is called exactly once across two requests."""
    ctx = two_orgs

    call_count = {"n": 0}

    import services.brigade_client as bc_mod

    class _CountingBrigadeClient:
        is_live = True

        def __init__(self, *args, **kwargs):
            pass

        async def fetch_entity_context(self, **kwargs):
            call_count["n"] += 1
            return {
                "success": True,
                "entity": {
                    "name": "meeting_ops_meeting_alpha",
                    "type": "Meeting",
                    "graph": "agent_meeting_ops_canonical",
                },
                "relationships": [],
                "related_entities": [],
            }

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(bc_mod, "BrigadeClient", _CountingBrigadeClient)

    url = f"/api/sessions/{ctx['sess_synced_pub']}/brigade-graph"
    r1 = client.get(url, headers=ctx["headers"])
    r2 = client.get(url, headers=ctx["headers"])
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()
    # Cached: Brigade fetch ran only once.
    assert call_count["n"] == 1, "expected response cache to absorb second call"


# ---------------------------------------------------------------------------
# 7. Cache invalidates on resync (brigade_synced_at bump)
# ---------------------------------------------------------------------------


def test_cache_invalidates_on_resync(client, two_orgs, monkeypatch):
    """When the writer re-runs and stamps a new brigade_synced_at, the
    cache key (which includes synced_at) changes implicitly and the
    next read hits Brigade again."""
    ctx = two_orgs

    call_count = {"n": 0}

    import services.brigade_client as bc_mod

    class _CountingBrigadeClient:
        is_live = True

        def __init__(self, *args, **kwargs):
            pass

        async def fetch_entity_context(self, **kwargs):
            call_count["n"] += 1
            return {
                "success": True,
                "entity": {
                    "name": "meeting_ops_meeting_alpha",
                    "type": "Meeting",
                    "graph": "agent_meeting_ops_canonical",
                },
                "relationships": [],
                "related_entities": [],
            }

        async def aclose(self) -> None:
            pass

    monkeypatch.setattr(bc_mod, "BrigadeClient", _CountingBrigadeClient)

    url = f"/api/sessions/{ctx['sess_synced_pub']}/brigade-graph"

    # First call - cache miss.
    r1 = client.get(url, headers=ctx["headers"])
    assert r1.status_code == 200
    assert call_count["n"] == 1

    # Bump brigade_synced_at on the session row to simulate a re-sync.
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        sess = (
            db.query(RecordingSession)
            .filter(RecordingSession.id == ctx["sess_synced_pk"])
            .first()
        )
        sess.brigade_synced_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    # Second call - cache key changed (synced_at differs), so Brigade
    # is hit again.
    r2 = client.get(url, headers=ctx["headers"])
    assert r2.status_code == 200
    assert call_count["n"] == 2, (
        "expected cache invalidation when brigade_synced_at bumped"
    )
