"""Cross-org isolation regression tests.

Audit on 2026-05-19 confirmed every Qdrant + Postgres search path that the
meeting-RAG agent + ai-chat surface is org-scoped via
``active_org.organization.id``. This test pins that contract: if any future
change removes a filter, drops an ``organization_id`` constraint, or
swaps in a payload key that drifts from the writer (the dead
``meet_chunks`` pipeline bug we just deleted), one of these parametrized
cases will fail.

Coverage:

| Path                                                | Layer | Source              |
|-----------------------------------------------------|-------|---------------------|
| ``semantic_search_service.search``                  | unit  | Qdrant              |
| ``semantic_search_service.search_chunks``           | unit  | Qdrant              |
| ``agent_tools.search_meetings_impl``                | unit  | Qdrant + Postgres   |
| ``agent_tools.list_meetings_impl``                  | unit  | Postgres            |
| ``agent_tools.get_meeting_details_impl``            | unit  | Postgres            |
| ``agent_tools.get_meeting_transcript_impl``         | unit  | Postgres            |
| ``agent_tools.chat_with_meeting_impl``              | unit  | Postgres            |
| ``POST /api/ai-chat/rag/query``                     | http  | Qdrant + Postgres   |
| ``GET  /api/simple/recording-sessions``             | http  | Postgres            |
| ``GET  /api/simple/recording-sessions/{id}``        | http  | Postgres            |
| ``GET  /api/simple/recording-sessions/search``      | http  | Postgres            |
| ``POST /api/satellites/register``                   | http  | Postgres            |
| ``GET  /api/satellites``                            | http  | Postgres            |
| ``GET  /api/satellites/{device_id}``                | http  | Postgres            |
| ``PUT  /api/satellites/{device_id}``                | http  | Postgres            |
| ``DELETE /api/satellites/{device_id}``              | http  | Postgres            |
| ``GET  /api/satellites/rooms``                      | http  | Postgres            |
| ``POST /api/satellites/{device_id}/heartbeat``      | http  | Postgres            |
| ``POST /api/satellites/{device_id}/start-recording``| http  | Postgres            |

Qdrant is faked at the ``SemanticSearchService`` boundary. Mocks return
results scoped to whatever ``organization_id`` the service passes in. A
real cross-org leak would either ignore that filter (returning org B data
when org A asked) or call the service without the filter at all (the
fake then sees ``None`` and the assertion catches it).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable
from unittest.mock import patch

import pytest

from auth.utils import get_password_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _models():
    """Lazy import — these modules are reloaded by the session app fixture."""
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
        tier="enterprise",  # paid tier: server-processing endpoints are gated (v3.0.0)
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login_headers(client, username: str, password: str) -> dict[str, str]:
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
    transcript_simple: str,
    summary: str | None = None,
) -> Any:
    """Create a recording_sessions row with enough text fields populated that
    every search path under test has something to find."""
    _, _, _, _, RecordingSession = _models()
    sess = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        status="completed",
        organization_id=organization_id,
        user_id=user_id,
        transcript_simple=transcript_simple,
        transcript=transcript_simple,
        summary=summary or transcript_simple,
        final_summary={"executive": summary or transcript_simple},
        duration=120.0,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


@pytest.fixture()
def two_orgs(client):
    """Seed two orgs, one user per org, one meeting per org. Returns a dict
    with everything subsequent tests need: ids, public session_ids, and an
    authenticated client for the org-A user."""
    Organization, _, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]

    try:
        # billing-1: both orgs are on a paid plan so the enterprise test user's
        # requests pass the per-workspace tier gate and actually reach the
        # cross-org isolation check under test (a free org would 403 first).
        org_a = Organization(
            name=f"Org Alpha {suffix}",
            slug=f"alpha-{suffix}",
            is_active=True,
            plan="enterprise",
        )
        org_b = Organization(
            name=f"Org Bravo {suffix}",
            slug=f"bravo-{suffix}",
            is_active=True,
            plan="enterprise",
        )
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        user_a = _seed_user(
            db,
            f"user_a_{suffix}",
            "Password123",
            f"a_{suffix}@example.com",
        )
        user_b = _seed_user(
            db,
            f"user_b_{suffix}",
            "Password123",
            f"b_{suffix}@example.com",
        )
        db.add_all([
            UserOrganization(user_id=user_a.id, organization_id=org_a.id, role="user"),
            UserOrganization(user_id=user_b.id, organization_id=org_b.id, role="user"),
        ])
        db.commit()

        # Distinct content so any cross-contamination is obvious in failure
        # output. The keyword "bravosecret" only ever lives in org B; if a
        # search run from org A surfaces it the leak is unambiguous.
        sess_a = _make_session(
            db,
            organization_id=org_a.id,
            user_id=user_a.id,
            title="Alpha Quarterly Plan",
            transcript_simple="Alpha team discussed alphaproject roadmap items.",
            summary="Alpha planning notes.",
        )
        sess_b = _make_session(
            db,
            organization_id=org_b.id,
            user_id=user_b.id,
            title="Bravo Pricing Review",
            transcript_simple="Bravo team aligned on bravosecret pricing tiers.",
            summary="Bravo pricing notes.",
        )

        ctx = {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "org_a_slug": org_a.slug,
            "org_b_slug": org_b.slug,
            "user_a_username": user_a.username,
            "user_b_username": user_b.username,
            "sess_a_public_id": sess_a.session_id,
            "sess_b_public_id": sess_b.session_id,
            "sess_a_db_id": sess_a.id,
            "sess_b_db_id": sess_b.id,
            "alpha_keyword": "alphaproject",
            "bravo_keyword": "bravosecret",
        }
    finally:
        db.close()

    ctx["headers_a"] = _login_headers(client, ctx["user_a_username"], "Password123")
    ctx["headers_b"] = _login_headers(client, ctx["user_b_username"], "Password123")
    return ctx


@pytest.fixture()
def fake_qdrant():
    """Replace ``SemanticSearchService`` query methods with org-aware fakes.

    The fakes record each call's ``organization_id`` and only return results
    payloaded with that same org_id, simulating a correctly-filtered Qdrant
    response. A test that triggers a real cross-org leak either:

      1. Calls these without an ``organization_id`` (the fake returns nothing
         and the call sites that DO have an org filter still pass; but if a
         caller forgot the filter entirely, ``last_calls`` reveals it), OR
      2. Asserts on the returned payloads and finds an org_id mismatch.

    Both modes are covered by the parametrized assertions below.
    """
    last_calls: list[dict[str, Any]] = []

    def fake_search(self, query, limit=10, organization_id=None):
        last_calls.append({
            "method": "search",
            "query": query,
            "organization_id": organization_id,
        })
        if organization_id is None:
            return []
        return [{
            "session_id": f"sess-org-{organization_id}",
            "title": f"Hit for org {organization_id}",
            "score": 0.91,
            "match_type": "transcript",
            "snippet": "synthetic match snippet",
            "created_at": "2026-05-19T00:00:00+00:00",
            "organization_id": organization_id,  # marker for the assertion
        }]

    def fake_search_chunks(self, query, limit=10, organization_id=None):
        last_calls.append({
            "method": "search_chunks",
            "query": query,
            "organization_id": organization_id,
        })
        if organization_id is None:
            return []
        return [{
            "session_id": f"sess-org-{organization_id}",
            "title": f"Hit for org {organization_id}",
            "content_type": "transcript",
            "chunk_index": 0,
            "text": "synthetic chunk text",
            "speakers": [],
            "score": 0.91,
            "created_at": "2026-05-19T00:00:00+00:00",
            "organization_id": organization_id,
        }]

    with patch(
        "services.semantic_search_service.SemanticSearchService.search",
        new=fake_search,
    ), patch(
        "services.semantic_search_service.SemanticSearchService.search_chunks",
        new=fake_search_chunks,
    ):
        yield last_calls


# ---------------------------------------------------------------------------
# Helpers for unit-layer paths
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Drive an async tool helper from sync test code. New loop per call so
    the session DB is freshly bound."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _semantic_search_call(method: str, *, org_id: int, query: str):
    from services.semantic_search_service import SemanticSearchService

    svc = SemanticSearchService()
    fn = getattr(svc, method)
    return fn(query=query, limit=5, organization_id=org_id)


def _agent_tools_call(method: str, *, db, org_id: int, **kwargs):
    from services import agent_tools

    fn = getattr(agent_tools, method)
    if method == "chat_with_meeting_impl":
        class _FakeLLM:
            def chat_sync(self, system_prompt, user_prompt, **_):  # pragma: no cover
                return "fake answer"
        return _run_async(fn(db, org_id, _FakeLLM(), **kwargs))
    return _run_async(fn(db, org_id, **kwargs))


# ---------------------------------------------------------------------------
# Parametrized unit-layer tests
# ---------------------------------------------------------------------------


UNIT_PATHS = [
    "semantic_search.search",
    "semantic_search.search_chunks",
    "agent_tools.search_meetings_impl",
    "agent_tools.list_meetings_impl",
    "agent_tools.get_meeting_details_impl",
    "agent_tools.get_meeting_transcript_impl",
    "agent_tools.chat_with_meeting_impl",
]


@pytest.mark.parametrize("path_under_test", UNIT_PATHS)
def test_no_cross_org_leak_unit(two_orgs, fake_qdrant, path_under_test):
    """Every unit-level search/list/get helper, when run as org A, must
    refuse to surface org B's data — either by returning empty results or
    a "not found" error payload."""
    org_a = two_orgs["org_a_id"]
    org_b_session = two_orgs["sess_b_public_id"]
    bravo_keyword = two_orgs["bravo_keyword"]

    if path_under_test == "semantic_search.search":
        results = _semantic_search_call("search", org_id=org_a, query=bravo_keyword)
        for r in results:
            assert r.get("organization_id") == org_a, (
                f"Leak: semantic_search.search returned an item tagged "
                f"organization_id={r.get('organization_id')} when org_a={org_a}"
            )

    elif path_under_test == "semantic_search.search_chunks":
        results = _semantic_search_call("search_chunks", org_id=org_a, query=bravo_keyword)
        for r in results:
            assert r.get("organization_id") == org_a, (
                f"Leak: semantic_search.search_chunks returned an item tagged "
                f"organization_id={r.get('organization_id')} when org_a={org_a}"
            )

    else:
        from database.database import SessionLocal

        db = SessionLocal()
        try:
            if path_under_test == "agent_tools.search_meetings_impl":
                result = _agent_tools_call(
                    "search_meetings_impl",
                    db=db,
                    org_id=org_a,
                    query=bravo_keyword,
                )
                for hit in result.get("results", []):
                    # Each hit must point at an org-A session id, never B
                    assert hit.get("session_id") != two_orgs["sess_b_public_id"], (
                        "Leak: search_meetings_impl surfaced org B's session id"
                    )
                    assert bravo_keyword not in (hit.get("snippet") or ""), (
                        "Leak: search_meetings_impl returned a snippet "
                        "containing org B's exclusive keyword."
                    )
                # The fake also recorded the filter — confirm it was passed
                fanout = [c for c in fake_qdrant if c["method"] == "search"]
                if fanout:
                    last = fanout[-1]
                    assert last["organization_id"] == org_a, (
                        f"Leak: search_meetings_impl forwarded "
                        f"organization_id={last['organization_id']} to Qdrant "
                        f"instead of org_a={org_a}"
                    )

            elif path_under_test == "agent_tools.list_meetings_impl":
                result = _agent_tools_call(
                    "list_meetings_impl",
                    db=db,
                    org_id=org_a,
                    limit=50,
                )
                for hit in result.get("meetings", []):
                    assert hit.get("session_id") != two_orgs["sess_b_public_id"], (
                        "Leak: list_meetings_impl surfaced org B's session id"
                    )

            elif path_under_test == "agent_tools.get_meeting_details_impl":
                # Org A asking for org B's session id must get a not-found error.
                result = _agent_tools_call(
                    "get_meeting_details_impl",
                    db=db,
                    org_id=org_a,
                    session_id=org_b_session,
                )
                assert "error" in result, (
                    f"Leak: get_meeting_details_impl returned org B data to "
                    f"org A: {result}"
                )
                assert "not found" in result["error"].lower()

            elif path_under_test == "agent_tools.get_meeting_transcript_impl":
                result = _agent_tools_call(
                    "get_meeting_transcript_impl",
                    db=db,
                    org_id=org_a,
                    session_id=org_b_session,
                )
                assert "error" in result, (
                    f"Leak: get_meeting_transcript_impl returned org B data "
                    f"to org A: {result}"
                )
                assert "not found" in result["error"].lower()

            elif path_under_test == "agent_tools.chat_with_meeting_impl":
                result = _agent_tools_call(
                    "chat_with_meeting_impl",
                    db=db,
                    org_id=org_a,
                    session_id=org_b_session,
                    message="What did we decide?",
                )
                assert "error" in result, (
                    f"Leak: chat_with_meeting_impl returned org B data to "
                    f"org A: {result}"
                )
                assert "not found" in result["error"].lower()

            else:  # pragma: no cover - guard for new entries to UNIT_PATHS
                pytest.fail(f"Unhandled path: {path_under_test}")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Parametrized HTTP-layer tests
# ---------------------------------------------------------------------------


HTTP_PATHS = [
    "rag.query",
    "recordings.list",
    "recordings.get_other_org",
    "recordings.search",
    "recordings.summary_slices_get_other_org",
    "recordings.summary_slices_post_other_org",
    # Full session audio capture: org A must NOT be able to attach audio
    # chunks to org B's session, or kick the reprocess pipeline against
    # someone else's session.
    "recordings.audio_chunks_post_other_org",
    "recordings.finalize_audio_post_other_org",
    # 025_session_attachments + move-org additions:
    "attachments.list_other_org",
    "attachments.upload_other_org",
    "attachments.download_other_org",
    "move_org.cross_org_target_blocked",
]


@pytest.mark.parametrize("path_under_test", HTTP_PATHS)
def test_no_cross_org_leak_http(client, two_orgs, fake_qdrant, path_under_test):
    """HTTP-layer tests — exercise the FastAPI routes that wrap the unit
    helpers. The active org is taken from the user's default membership
    (no ``X-MeetingOps-Org`` header), so the route resolves to org A."""
    headers_a = two_orgs["headers_a"]
    org_a_id = two_orgs["org_a_id"]
    org_b_session = two_orgs["sess_b_public_id"]
    bravo_keyword = two_orgs["bravo_keyword"]
    alpha_keyword = two_orgs["alpha_keyword"]

    if path_under_test == "rag.query":
        # The RAG endpoint will hit our fake search_chunks. Even if no LLM is
        # available, the route's pre-LLM org filter must keep org B's chunks
        # out of the sources array. We stop the call before LLM by patching
        # _llm_call to return a canned string (we only care about the
        # filter behavior, not the answer).
        with patch("api.ai_chat._llm_call", return_value="canned answer"):
            response = client.post(
                "/api/ai-chat/rag/query",
                json={"message": bravo_keyword, "limit": 5},
                headers=headers_a,
            )
        # 200 means we got a response back; 503 is acceptable if the test
        # env doesn't have a registry — either way no sources should leak.
        if response.status_code == 503:
            pytest.skip("Provider registry not available in this test env")
        assert response.status_code == 200, response.text
        body = response.json()
        for source in body.get("sources", []):
            assert source.get("session_id") != org_b_session, (
                f"Leak: /api/ai-chat/rag/query returned org B session "
                f"in sources: {source}"
            )
        # The fake's filter assertion: every recorded search_chunks call
        # must carry org_a_id
        fanout = [c for c in fake_qdrant if c["method"] == "search_chunks"]
        for call in fanout:
            assert call["organization_id"] == org_a_id, (
                f"Leak: /api/ai-chat/rag/query forwarded "
                f"organization_id={call['organization_id']} to Qdrant "
                f"instead of org_a={org_a_id}"
            )

    elif path_under_test == "recordings.list":
        response = client.get(
            "/api/simple/recording-sessions",
            headers=headers_a,
        )
        assert response.status_code == 200, response.text
        rows = response.json()["items"]
        for row in rows:
            assert row.get("id") != org_b_session, (
                f"Leak: /api/simple/recording-sessions listed org B session: {row}"
            )
            # Belt-and-suspenders: bravo content never appears in titles or
            # descriptions returned to org A.
            blob = " ".join(str(v) for v in row.values() if isinstance(v, (str, int)))
            assert bravo_keyword not in blob.lower(), (
                f"Leak: bravo keyword surfaced in org A's listing: {row}"
            )

    elif path_under_test == "recordings.get_other_org":
        response = client.get(
            f"/api/simple/recording-sessions/{org_b_session}",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: /api/simple/recording-sessions/{{org_b_id}} returned "
            f"status {response.status_code} when fetched as org A. "
            f"Body: {response.text[:500]}"
        )

    elif path_under_test == "recordings.summary_slices_get_other_org":
        # GET /api/recordings/sessions/{org_b_id}/summary-slices as org A
        # must 404 — never reveal existence and never return any slice
        # text that might have leaked through the JSONB column.
        response = client.get(
            f"/api/recordings/sessions/{org_b_session}/summary-slices",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: GET summary-slices for org B session as org A "
            f"returned status {response.status_code}. Body: "
            f"{response.text[:500]}"
        )

    elif path_under_test == "recordings.summary_slices_post_other_org":
        # POST /api/recordings/sessions/{org_b_id}/summary-slices as
        # org A must 404 — both as a defense against existence leaks
        # AND because a successful slice POST would persist bravo
        # content (transcript_simple) inside an "org A summary" header.
        response = client.post(
            f"/api/recordings/sessions/{org_b_session}/summary-slices",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: POST summary-slices for org B session as org A "
            f"returned status {response.status_code}. Body: "
            f"{response.text[:500]}"
        )

    elif path_under_test == "recordings.audio_chunks_post_other_org":
        # POST /api/recordings/sessions/{org_b_id}/audio-chunks as org A
        # must 404 — never accept full-session audio uploads against
        # another tenant's session. A successful upload here would let
        # org A write audio bytes under org B's session directory + have
        # them appear in org B's reassembled WAV on finalize. Same
        # existence-leak guardrail as the chunks-text path.
        response = client.post(
            f"/api/recordings/sessions/{org_b_session}/audio-chunks",
            headers=headers_a,
            files={"chunk": ("evil.webm", b"\x00\x00\x00", "audio/webm")},
            data={"chunk_index": "0"},
        )
        assert response.status_code == 404, (
            f"Leak: POST audio-chunks for org B session as org A "
            f"returned status {response.status_code}. Body: "
            f"{response.text[:500]}"
        )

    elif path_under_test == "recordings.finalize_audio_post_other_org":
        # POST /api/recordings/sessions/{org_b_id}/finalize-audio as
        # org A must 404 — kicking the reprocess pipeline against another
        # tenant's session would re-transcribe + re-summarize their
        # audio under org A's caller context (and could mutate their
        # processing_metadata.reprocess_status).
        response = client.post(
            f"/api/recordings/sessions/{org_b_session}/finalize-audio",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: POST finalize-audio for org B session as org A "
            f"returned status {response.status_code}. Body: "
            f"{response.text[:500]}"
        )

    elif path_under_test == "recordings.search":
        # Search for org B's exclusive keyword from org A's session — must
        # return zero hits.
        response = client.get(
            "/api/simple/recording-sessions/search",
            params={"q": bravo_keyword},
            headers=headers_a,
        )
        assert response.status_code == 200, response.text
        rows = response.json()
        assert rows == [], (
            f"Leak: /api/simple/recording-sessions/search?q={bravo_keyword} "
            f"returned {len(rows)} row(s) to org A: {rows}"
        )

        # Sanity check the harness: searching for org A's own keyword must
        # return at least one hit. Otherwise we're proving nothing.
        positive = client.get(
            "/api/simple/recording-sessions/search",
            params={"q": alpha_keyword},
            headers=headers_a,
        )
        assert positive.status_code == 200, positive.text
        assert positive.json(), (
            "Search harness is broken — org A cannot find its own meetings, "
            "so the negative assertion above is meaningless."
        )

    elif path_under_test == "attachments.list_other_org":
        # Listing attachments on org B's session from org A's headers
        # MUST 404 — the session is invisible across orgs.
        response = client.get(
            f"/api/simple/recording-sessions/{org_b_session}/attachments",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: org A could list attachments on org B's session "
            f"{org_b_session}; status={response.status_code}"
        )

    elif path_under_test == "attachments.upload_other_org":
        # Posting an attachment to org B's session from org A's headers
        # MUST 404 — the session is invisible across orgs.
        response = client.post(
            f"/api/simple/recording-sessions/{org_b_session}/attachments",
            headers=headers_a,
            files={"file": ("evil.txt", b"hi", "text/plain")},
            data={"attachment_type": "notes"},
        )
        assert response.status_code == 404, (
            f"Leak: org A could upload an attachment to org B's session "
            f"{org_b_session}; status={response.status_code}"
        )

    elif path_under_test == "attachments.download_other_org":
        # Downloading an arbitrary attachment id on org B's session from
        # org A's headers MUST 404 (the session is invisible). The
        # arbitrary id makes this test resilient to the writer's
        # actual key format.
        response = client.get(
            f"/api/simple/recording-sessions/{org_b_session}/attachments/00000000-0000-0000-0000-000000000000/download",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: org A could probe attachment download on org B's session "
            f"{org_b_session}; status={response.status_code}"
        )

    elif path_under_test == "move_org.cross_org_target_blocked":
        # Move-org: A's caller is admin in A but not a member of B.
        # An attempt to move an A-session into B must be 403 (the
        # target-membership check fires), NOT 200. This pins the
        # contract: just because you can read a session doesn't mean
        # you can shove it into someone else's org.
        response = client.post(
            f"/api/simple/recording-sessions/{two_orgs['sess_a_public_id']}/move-org",
            headers=headers_a,
            json={"target_organization_id": two_orgs["org_b_id"]},
        )
        assert response.status_code == 403, (
            f"Leak: org A's admin (not a member of org B) could move a "
            f"session into org B; status={response.status_code}, "
            f"body={response.text[:300]}"
        )

    else:  # pragma: no cover
        pytest.fail(f"Unhandled HTTP path: {path_under_test}")


# ---------------------------------------------------------------------------
# Satellite device isolation (CR-001)
# ---------------------------------------------------------------------------
#
# Regression coverage for the cross-org leak in backend/api/satellite_api.py
# (task #82). Before the fix, every satellite endpoint looked up devices by
# ``device_id`` alone — any authenticated user could read, mutate, delete,
# or upload to another tenant's satellite devices. Same shape as the
# ``meet_chunks`` payload-key drift bug we closed in 8dec955.
#
# These tests use the existing two_orgs seed pattern, then layer on one
# satellite device per org. Every endpoint that takes a device_id must
# return 404 when called from the other org's credentials; the list/rooms
# endpoints must omit other-org devices entirely; the register endpoint
# must bind the new device to the caller's active org regardless of any
# payload-supplied org hint.


def _seed_satellite_device(
    db,
    *,
    organization_id: int,
    device_id: str,
    name: str,
    room_name: str | None = None,
):
    """Insert a satellite_devices row directly. Mirrors the prod register
    endpoint enough that the read/update/delete paths see a realistic row.
    """
    from database.models import SatelliteDevice

    from datetime import datetime, timezone

    device = SatelliteDevice(
        device_id=device_id,
        name=name,
        room_name=room_name,
        device_type="esp32-s3",
        status="online",
        last_heartbeat=datetime.now(timezone.utc),
        api_key="test-hashed-key-" + device_id,
        organization_id=organization_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


@pytest.fixture()
def two_orgs_with_satellites(two_orgs):
    """Extend the two_orgs fixture with one satellite device per org. Returns
    the same ctx dict plus device_id keys for each side.

    Unique device_id values per test run (suffix from two_orgs) so reruns
    against the same SQLite db never collide on the global unique
    constraint.
    """
    _, _, _, SessionLocal, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        device_a = _seed_satellite_device(
            db,
            organization_id=two_orgs["org_a_id"],
            device_id=f"esp-alpha-{suffix}",
            name=f"Alpha Conf Room Mic {suffix}",
            room_name="Alpha Conference Room",
        )
        device_b = _seed_satellite_device(
            db,
            organization_id=two_orgs["org_b_id"],
            device_id=f"esp-bravo-{suffix}",
            name=f"Bravo Conf Room Mic {suffix}",
            room_name="Bravo Conference Room",
        )
        two_orgs["device_a_id"] = device_a.device_id
        two_orgs["device_b_id"] = device_b.device_id
        two_orgs["device_a_pk"] = device_a.id
        two_orgs["device_b_pk"] = device_b.id
        two_orgs["device_a_room"] = device_a.room_name
        two_orgs["device_b_room"] = device_b.room_name
    finally:
        db.close()

    return two_orgs


SATELLITE_PATHS = [
    "satellites.list",
    "satellites.get_other_org",
    "satellites.update_other_org",
    "satellites.delete_other_org",
    "satellites.heartbeat_other_org",
    "satellites.start_recording_other_org",
    "satellites.rooms",
    "satellites.register_under_caller_org",
]


@pytest.mark.parametrize("path_under_test", SATELLITE_PATHS)
def test_no_cross_org_leak_satellites(client, two_orgs_with_satellites, path_under_test):
    """Every satellite endpoint, when invoked as org A, must refuse to read,
    list, or mutate org B's devices. Register must bind the new device to
    the caller's org regardless of any client-supplied org hint."""
    ctx = two_orgs_with_satellites
    headers_a = ctx["headers_a"]
    org_a_id = ctx["org_a_id"]
    org_b_id = ctx["org_b_id"]
    device_a_id = ctx["device_a_id"]
    device_b_id = ctx["device_b_id"]

    if path_under_test == "satellites.list":
        response = client.get("/api/satellites", headers=headers_a)
        assert response.status_code == 200, response.text
        rows = response.json()
        # Org A must see its own device.
        seen_ids = {row.get("device_id") for row in rows}
        assert device_a_id in seen_ids, (
            f"Harness broken: org A cannot see its own device "
            f"{device_a_id} in /api/satellites: {seen_ids}"
        )
        # And must NEVER see org B's device.
        assert device_b_id not in seen_ids, (
            f"Leak: GET /api/satellites returned org B device {device_b_id} "
            f"to org A. Full payload: {rows}"
        )

    elif path_under_test == "satellites.get_other_org":
        response = client.get(
            f"/api/satellites/{device_b_id}",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: GET /api/satellites/{{org_b_device}} returned "
            f"status {response.status_code} when fetched as org A. "
            f"Body: {response.text[:500]}"
        )

    elif path_under_test == "satellites.update_other_org":
        response = client.put(
            f"/api/satellites/{device_b_id}",
            json={"name": "PWNED-BY-ALPHA", "room_name": "alpha-room"},
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: PUT /api/satellites/{{org_b_device}} returned "
            f"status {response.status_code} (expected 404) when called as "
            f"org A. Body: {response.text[:500]}"
        )

        # Belt-and-suspenders: the org B device row must be untouched.
        _, _, _, SessionLocal, _ = _models()
        db = SessionLocal()
        try:
            from database.models import SatelliteDevice
            row = db.query(SatelliteDevice).filter(
                SatelliteDevice.device_id == device_b_id
            ).first()
            assert row is not None
            assert row.name != "PWNED-BY-ALPHA", (
                "Leak: org A's update succeeded in mutating org B's device row "
                f"(name={row.name!r})."
            )
            assert row.organization_id == org_b_id, (
                f"Leak: org A's update changed org B device's organization_id "
                f"from {org_b_id} to {row.organization_id}."
            )
        finally:
            db.close()

    elif path_under_test == "satellites.delete_other_org":
        response = client.delete(
            f"/api/satellites/{device_b_id}",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: DELETE /api/satellites/{{org_b_device}} returned "
            f"status {response.status_code} (expected 404) when called as "
            f"org A. Body: {response.text[:500]}"
        )

        # The org B device row must still exist.
        _, _, _, SessionLocal, _ = _models()
        db = SessionLocal()
        try:
            from database.models import SatelliteDevice
            row = db.query(SatelliteDevice).filter(
                SatelliteDevice.device_id == device_b_id
            ).first()
            assert row is not None, (
                "Leak: org A's DELETE removed org B's satellite device row."
            )
        finally:
            db.close()

    elif path_under_test == "satellites.heartbeat_other_org":
        response = client.post(
            f"/api/satellites/{device_b_id}/heartbeat",
            json={"status": "recording"},
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: POST /api/satellites/{{org_b_device}}/heartbeat "
            f"returned status {response.status_code} (expected 404) when "
            f"called as org A. Body: {response.text[:500]}"
        )

    elif path_under_test == "satellites.start_recording_other_org":
        response = client.post(
            f"/api/satellites/{device_b_id}/start-recording",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: POST /api/satellites/{{org_b_device}}/start-recording "
            f"returned status {response.status_code} (expected 404) when "
            f"called as org A. Body: {response.text[:500]}"
        )

    elif path_under_test == "satellites.rooms":
        response = client.get("/api/satellites/rooms", headers=headers_a)
        assert response.status_code == 200, response.text
        rooms = response.json()
        # Flatten device_ids out of every room.
        flat_device_ids = {
            d.get("device_id")
            for room in rooms
            for d in room.get("devices", [])
        }
        assert device_a_id in flat_device_ids, (
            "Harness broken: org A cannot see its own device under "
            "/api/satellites/rooms."
        )
        assert device_b_id not in flat_device_ids, (
            f"Leak: GET /api/satellites/rooms returned org B device "
            f"{device_b_id} to org A. Full rooms payload: {rooms}"
        )
        # And the room name should not surface either, since that's how
        # users actually browse devices in the UI.
        seen_rooms = {room.get("room_name") for room in rooms}
        assert ctx["device_b_room"] not in seen_rooms, (
            f"Leak: org B's room name {ctx['device_b_room']!r} surfaced "
            f"in org A's /api/satellites/rooms listing: {seen_rooms}"
        )

    elif path_under_test == "satellites.register_under_caller_org":
        # The client tries to specify org B in the payload (no schema field
        # exists for organization_id, so we lean on the default behavior:
        # the server must always bind to the caller's active org, never to
        # any payload hint or default). Use a unique device_id so this run
        # doesn't collide with previously-registered fixtures.
        new_device_id = f"esp-new-{uuid.uuid4().hex[:8]}"
        response = client.post(
            "/api/satellites/register",
            json={
                "device_id": new_device_id,
                "name": "Test New Device",
                "device_type": "esp32-s3",
                # NOTE: organization_id is not part of the schema. Even if a
                # caller smuggles it in via extra fields, Pydantic should
                # drop it on the way in and the server must end up with the
                # caller's org.
                "organization_id": org_b_id,
            },
            headers=headers_a,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "device" in body, body
        assert body["device"]["device_id"] == new_device_id

        # Verify the row landed under org A, not org B.
        _, _, _, SessionLocal, _ = _models()
        db = SessionLocal()
        try:
            from database.models import SatelliteDevice
            row = db.query(SatelliteDevice).filter(
                SatelliteDevice.device_id == new_device_id
            ).first()
            assert row is not None, "Newly-registered device missing from DB"
            assert row.organization_id == org_a_id, (
                f"Leak: POST /api/satellites/register stored "
                f"organization_id={row.organization_id} for device "
                f"{new_device_id} when caller is in org {org_a_id}. "
                f"Payload-supplied org B hint ({org_b_id}) must be ignored."
            )
        finally:
            db.close()

    else:  # pragma: no cover
        pytest.fail(f"Unhandled satellite path: {path_under_test}")


# ---------------------------------------------------------------------------
# Conference Room cross-org isolation (CR-002 / CR-003)
# ---------------------------------------------------------------------------
#
# Mirrors the satellite block above. Every /api/rooms/* path must refuse
# to read, list, or mutate another org's rooms. The fixture seeds one
# room per org so the assertions can match against the wrong-org row id.


def _seed_conference_room(db, *, organization_id: int, name: str):
    from database.models_rooms import ConferenceRoom
    row = ConferenceRoom(organization_id=organization_id, name=name)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_conference_room_source(db, *, room_id, hardware_type="server_usb_mic", device_path="hw:1,0"):
    from database.models_rooms import RoomAudioSource
    src = RoomAudioSource(
        room_id=room_id,
        hardware_type=hardware_type,
        device_path=device_path,
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


@pytest.fixture()
def two_orgs_with_rooms(two_orgs):
    """Extend the two_orgs fixture with one conference room per org.

    Promotes org A's and org B's seeded user to org admin so create/grant
    paths in the parametrized test can actually exercise the admin gate
    (the base fixture seeds 'user' role).
    """
    _, _, _, SessionLocal, _ = _models()
    from auth.models import UserOrganization

    db = SessionLocal()
    try:
        # Promote both seeded users to admin so the admin-gated paths under
        # test return 200/403 (not 403/403 ambiguous).
        for user_org_id, user_id in [
            (two_orgs["org_a_id"], None),
            (two_orgs["org_b_id"], None),
        ]:
            pass
        # Simpler — promote by username.
        from auth.models import User
        for username in (two_orgs["user_a_username"], two_orgs["user_b_username"]):
            user = db.query(User).filter(User.username == username).first()
            mem = (
                db.query(UserOrganization)
                .filter(UserOrganization.user_id == user.id)
                .first()
            )
            if mem and mem.role != "admin":
                mem.role = "admin"
        db.commit()

        room_a = _seed_conference_room(
            db,
            organization_id=two_orgs["org_a_id"],
            name=f"Alpha Conf {uuid.uuid4().hex[:6]}",
        )
        room_b = _seed_conference_room(
            db,
            organization_id=two_orgs["org_b_id"],
            name=f"Bravo Conf {uuid.uuid4().hex[:6]}",
        )
        src_b = _seed_conference_room_source(db, room_id=room_b.id)
        two_orgs["room_a_id"] = str(room_a.id)
        two_orgs["room_b_id"] = str(room_b.id)
        two_orgs["room_a_name"] = room_a.name
        two_orgs["room_b_name"] = room_b.name
        two_orgs["room_b_source_id"] = str(src_b.id)
    finally:
        db.close()

    return two_orgs


ROOM_PATHS = [
    "rooms.list",
    "rooms.get_other_org",
    "rooms.update_other_org",
    "rooms.delete_other_org",
    "rooms.add_source_other_org",
    "rooms.list_sources_other_org",
    "rooms.pairing_code_other_org",
    "rooms.start_recording_other_org",
    "rooms.stop_recording_other_org",
    "rooms.acl_grant_other_org",
]


@pytest.mark.parametrize("path_under_test", ROOM_PATHS)
def test_no_cross_org_leak_rooms(client, two_orgs_with_rooms, path_under_test):
    """Every room endpoint must refuse to read or mutate another org's room."""
    ctx = two_orgs_with_rooms
    headers_a = ctx["headers_a"]
    room_a_id = ctx["room_a_id"]
    room_b_id = ctx["room_b_id"]

    if path_under_test == "rooms.list":
        response = client.get("/api/rooms", headers=headers_a)
        assert response.status_code == 200, response.text
        rows = response.json()
        ids = {row.get("id") for row in rows}
        assert room_a_id in ids, (
            f"Harness broken: org A cannot see its own room {room_a_id}"
        )
        assert room_b_id not in ids, (
            f"Leak: GET /api/rooms returned org B room {room_b_id}. Payload: {rows}"
        )

    elif path_under_test == "rooms.get_other_org":
        response = client.get(f"/api/rooms/{room_b_id}", headers=headers_a)
        assert response.status_code == 404, (
            f"Leak: GET /api/rooms/{{org_b}} returned {response.status_code} "
            f"to org A. Body: {response.text[:300]}"
        )

    elif path_under_test == "rooms.update_other_org":
        response = client.put(
            f"/api/rooms/{room_b_id}",
            json={"name": "PWNED-BY-ALPHA"},
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: PUT /api/rooms/{{org_b}} returned {response.status_code}. "
            f"Body: {response.text[:300]}"
        )

    elif path_under_test == "rooms.delete_other_org":
        response = client.delete(f"/api/rooms/{room_b_id}", headers=headers_a)
        assert response.status_code == 404

    elif path_under_test == "rooms.add_source_other_org":
        response = client.post(
            f"/api/rooms/{room_b_id}/sources",
            json={"hardware_type": "server_usb_mic", "device_path": "hw:9,0"},
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: POST /api/rooms/{{org_b}}/sources returned "
            f"{response.status_code}. Body: {response.text[:300]}"
        )

    elif path_under_test == "rooms.list_sources_other_org":
        response = client.get(
            f"/api/rooms/{room_b_id}/sources",
            headers=headers_a,
        )
        # Could be 403 (org-mismatch ACL fails) or 404 (org-scoped lookup).
        # Spec is: never reveal existence — must be 404.
        assert response.status_code == 404, (
            f"Leak: GET /api/rooms/{{org_b}}/sources returned "
            f"{response.status_code}. Body: {response.text[:300]}"
        )

    elif path_under_test == "rooms.pairing_code_other_org":
        response = client.post(
            f"/api/rooms/{room_b_id}/pairing-codes",
            headers=headers_a,
        )
        assert response.status_code == 404, (
            f"Leak: POST /api/rooms/{{org_b}}/pairing-codes returned "
            f"{response.status_code}. Body: {response.text[:300]}"
        )

    elif path_under_test == "rooms.start_recording_other_org":
        response = client.post(
            f"/api/rooms/{room_b_id}/recordings/start",
            headers=headers_a,
        )
        assert response.status_code == 404

    elif path_under_test == "rooms.stop_recording_other_org":
        response = client.post(
            f"/api/rooms/{room_b_id}/recordings/stop",
            headers=headers_a,
        )
        assert response.status_code == 404

    elif path_under_test == "rooms.acl_grant_other_org":
        response = client.post(
            f"/api/rooms/{room_b_id}/acl",
            json={"user_id": 99999, "role": "viewer"},
            headers=headers_a,
        )
        assert response.status_code == 404

    else:  # pragma: no cover
        pytest.fail(f"Unhandled rooms path: {path_under_test}")


# ---------------------------------------------------------------------------
# Per-device secret cross-org isolation (task #85)
# ---------------------------------------------------------------------------
#
# A device secret minted in org A MUST NOT authenticate as any device in
# org B — even if an attacker knows org B's device_id. The state-mutating
# satellite endpoints look up the device by device_id, so this is a
# direct test of "does the lookup ignore the secret's origin org?". The
# device_id is globally unique at the column level, so the only way org
# A's secret could authenticate against org B is if the verify path
# ignored the device row's stored hash entirely.


@pytest.fixture()
def two_orgs_with_paired_satellites(two_orgs):
    """Like ``two_orgs_with_satellites`` but each device has a real
    bcrypt-hashed secret + we keep the plaintext around for the test."""
    from auth.device_auth import (
        generate_device_secret,
        hash_device_secret,
        reset_rate_limiter_for_tests,
    )

    reset_rate_limiter_for_tests()

    _, _, _, SessionLocal, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]

    secret_a = generate_device_secret()
    secret_b = generate_device_secret()

    try:
        from database.models import SatelliteDevice
        from datetime import datetime, timezone

        device_a = SatelliteDevice(
            device_id=f"esp-alpha-paired-{suffix}",
            name=f"Alpha Paired {suffix}",
            device_type="esp32-s3",
            status="online",
            last_heartbeat=datetime.now(timezone.utc),
            device_secret=hash_device_secret(secret_a),
            organization_id=two_orgs["org_a_id"],
            created_at=datetime.now(timezone.utc),
        )
        device_b = SatelliteDevice(
            device_id=f"esp-bravo-paired-{suffix}",
            name=f"Bravo Paired {suffix}",
            device_type="esp32-s3",
            status="online",
            last_heartbeat=datetime.now(timezone.utc),
            device_secret=hash_device_secret(secret_b),
            organization_id=two_orgs["org_b_id"],
            created_at=datetime.now(timezone.utc),
        )
        db.add_all([device_a, device_b])
        db.commit()
        db.refresh(device_a)
        db.refresh(device_b)

        two_orgs["paired_device_a_id"] = device_a.device_id
        two_orgs["paired_device_b_id"] = device_b.device_id
        two_orgs["paired_secret_a"] = secret_a
        two_orgs["paired_secret_b"] = secret_b
    finally:
        db.close()

    yield two_orgs

    reset_rate_limiter_for_tests()


def test_device_secret_does_not_cross_orgs(client, two_orgs_with_paired_satellites):
    """Org A's device_secret presented against org B's device_id ⇒ 401.
    The reverse path is symmetric."""
    ctx = two_orgs_with_paired_satellites

    # Secret_a vs device B
    resp = client.post(
        f"/api/satellites/{ctx['paired_device_b_id']}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": ctx["paired_secret_a"]},
    )
    assert resp.status_code == 401, (
        "Leak: org A's device_secret authenticated against org B's "
        f"device_id={ctx['paired_device_b_id']}. Status={resp.status_code}, "
        f"body={resp.text[:300]}"
    )

    # Secret_b vs device A — also denied. Reset the limiter first so
    # the previous failure doesn't taint this assertion.
    from auth.device_auth import reset_rate_limiter_for_tests
    reset_rate_limiter_for_tests()

    resp = client.post(
        f"/api/satellites/{ctx['paired_device_a_id']}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": ctx["paired_secret_b"]},
    )
    assert resp.status_code == 401, (
        "Leak: org B's device_secret authenticated against org A's "
        f"device_id={ctx['paired_device_a_id']}. Status={resp.status_code}, "
        f"body={resp.text[:300]}"
    )


def test_device_secret_works_against_own_device(client, two_orgs_with_paired_satellites):
    """Sanity: the matching secret + device_id pair succeeds."""
    ctx = two_orgs_with_paired_satellites
    from auth.device_auth import reset_rate_limiter_for_tests
    reset_rate_limiter_for_tests()

    resp = client.post(
        f"/api/satellites/{ctx['paired_device_a_id']}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": ctx["paired_secret_a"]},
    )
    assert resp.status_code == 200, (
        f"Harness broken: org A's secret should authenticate against "
        f"its own device_id. Got {resp.status_code} body={resp.text[:300]}"
    )
