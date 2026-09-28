"""Tests for the server-rolled summary slice store.

Covers:
  * Auto-trigger fires for room sessions when the transcript grows past
    the configured word threshold.
  * Auto-trigger is a no-op for non-room (browser always-on) sessions —
    those still roll slices client-side and must not get server slices
    inserted under their feet.
  * Manual POST endpoint creates + persists a slice, returns the
    serialized shape.
  * GET endpoint returns slices sorted oldest-first.
  * Empty / legacy sessions return an empty list (not 500, not 404).
  * Org-scoping — cross-org access is 404 (existence-leak guard).

LLM is monkeypatched at the provider registry boundary so the tests
never hit an external service. We assert on the side effects (slice
text matches what the fake returns; metadata is populated) rather than
on token stream contents.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from auth.utils import get_password_hash


class _FakeLLM:
    """Returns a deterministic short summary so tests can assert on text."""

    model = "fake-llm-1"

    async def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 500,
        temperature: float = 0.7,
        extra_params: Any = None,
    ) -> str:
        # Echo just enough of the prompt to make the assertion meaningful
        # without coupling to the exact prompt wording.
        return "ROOMSUMMARY: aligned on roadmap, decisions captured."


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession

    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _seed_user(db, username: str, password: str, email: str) -> Any:
    from auth.models import User

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


def _login_headers(client, username: str, password: str, org_slug: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {
        "Authorization": f"Bearer {response.json()['access_token']}",
        "X-MeetingOps-Org": org_slug,
    }


def _seed_session(
    db,
    *,
    organization_id: int,
    user_id: int,
    transcript_simple: str,
    room_id: uuid.UUID | None = None,
    title: str = "Slice Test",
) -> Any:
    _, _, _, _, RecordingSession = _models()
    sess = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        status="recording",
        mode="always_on",
        organization_id=organization_id,
        user_id=user_id,
        transcript_simple=transcript_simple,
        duration=120.0,
        room_id=room_id,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def _seed_room(db, organization_id: int, name: str = "Boardroom") -> Any:
    from database.models_rooms import ConferenceRoom

    room = ConferenceRoom(
        organization_id=organization_id,
        name=name,
        status="idle",
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    return room


@pytest.fixture()
def slice_world(client):
    """Two orgs with one user each; room in org A; sessions in both."""
    Organization, _, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        org_a = Organization(name=f"Alpha {suffix}", slug=f"alpha-{suffix}", is_active=True, plan="pro")
        org_b = Organization(name=f"Bravo {suffix}", slug=f"bravo-{suffix}", is_active=True, plan="pro")
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        user_a = _seed_user(db, f"slice_a_{suffix}", "Password123", f"a_{suffix}@x.com")
        user_b = _seed_user(db, f"slice_b_{suffix}", "Password123", f"b_{suffix}@x.com")
        db.add_all([
            UserOrganization(user_id=user_a.id, organization_id=org_a.id, role="admin"),
            UserOrganization(user_id=user_b.id, organization_id=org_b.id, role="admin"),
        ])
        db.commit()

        # Summary-slice POST is the qwen36_summary (Pro) gate — both the user
        # tier AND the active-org plan must cover it (billing-1).
        user_a.tier = "pro"
        user_b.tier = "pro"
        db.commit()

        room_a = _seed_room(db, org_a.id, name=f"Room {suffix}")

        ctx = {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "org_a_slug": org_a.slug,
            "org_b_slug": org_b.slug,
            "user_a_id": user_a.id,
            "user_b_id": user_b.id,
            "user_a_username": user_a.username,
            "user_b_username": user_b.username,
            "room_a_id": room_a.id,
        }
    finally:
        db.close()

    ctx["headers_a"] = _login_headers(client, ctx["user_a_username"], "Password123", ctx["org_a_slug"])
    ctx["headers_b"] = _login_headers(client, ctx["user_b_username"], "Password123", ctx["org_b_slug"])
    return ctx


@pytest.fixture()
def fake_llm(monkeypatch):
    """Patch the provider registry so summary calls return _FakeLLM."""
    from services.providers import registry as registry_module

    monkeypatch.setattr(
        registry_module.ProviderRegistry,
        "get_llm",
        lambda self, org_id, task="quality": _FakeLLM(),
    )
    yield


# ---------------------------------------------------------------------------
# Auto-trigger (service layer)
# ---------------------------------------------------------------------------


def test_auto_trigger_fires_for_room_session_past_threshold(
    slice_world, fake_llm, monkeypatch
):
    """Auto-trigger creates a slice when the room session passes the
    configured word threshold."""
    import asyncio

    from database.database import SessionLocal
    from services import summary_slices as ss

    # Lower the threshold so the test transcript trips it.
    monkeypatch.setattr(ss, "ROOM_SLICE_TRIGGER_WORDS", 10)

    transcript = " ".join(f"word{i}" for i in range(40))  # 40 words

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple=transcript,
            room_id=slice_world["room_a_id"],
        )
        result = asyncio.run(
            ss.maybe_auto_trigger_slice(db, session, trigger_words=10)
        )
        assert result is not None, "expected auto-trigger to fire"
        assert result["triggered_by"] == "room-recorder"
        assert result["word_range_start"] == 0
        assert result["word_range_end"] == 40
        assert "ROOMSUMMARY" in result["text"]
        db.commit()

        # The slice is now persisted on the session's processing_metadata.
        db.refresh(session)
        slices = (session.processing_metadata or {}).get("summary_slices") or []
        assert len(slices) == 1
        assert slices[0]["id"] == result["id"]
    finally:
        db.close()


def test_auto_trigger_below_threshold_returns_none(slice_world, fake_llm, monkeypatch):
    """No slice produced when delta is below the trigger threshold."""
    import asyncio

    from database.database import SessionLocal
    from services import summary_slices as ss

    monkeypatch.setattr(ss, "ROOM_SLICE_TRIGGER_WORDS", 100)

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple="only ten words here below threshold for sure okay",
            room_id=slice_world["room_a_id"],
        )
        result = asyncio.run(
            ss.maybe_auto_trigger_slice(db, session, trigger_words=100)
        )
        assert result is None
    finally:
        db.close()


def test_auto_trigger_fires_for_browser_always_on_session(slice_world, fake_llm):
    """Browser always-on sessions (room_id=None) DO auto-trigger server-
    side slices now. Previously they rolled in the browser with Qwen 3
    0.6B, which choked on incremental summarization with previous-
    summary context (slice stack repeated early content). Moved onto
    the server's Qwen 3.6 35B-A3B-Vision path 2026-05-21.

    Note the ``triggered_by`` discriminator: ``auto-words`` for browser
    always-on, ``room-recorder`` for room sessions — analytics can still
    see the split, the LLM model is the same."""
    import asyncio

    from database.database import SessionLocal
    from services import summary_slices as ss

    transcript = " ".join(f"word{i}" for i in range(40))
    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple=transcript,
            room_id=None,
        )
        result = asyncio.run(
            ss.maybe_auto_trigger_slice(db, session, trigger_words=10)
        )
        assert result is not None, (
            "Browser always-on session must auto-trigger now that the "
            "room_id gate is removed."
        )
        assert result["triggered_by"] == "auto-words", (
            f"Expected triggered_by='auto-words' for browser always-on, "
            f"got {result['triggered_by']!r}"
        )
        assert result["word_range_start"] == 0
        assert result["word_range_end"] == 40
        db.commit()

        # Slice persisted on processing_metadata just like room sessions.
        db.refresh(session)
        slices = (session.processing_metadata or {}).get("summary_slices") or []
        assert len(slices) == 1
        assert slices[0]["id"] == result["id"]
    finally:
        db.close()


def test_auto_trigger_uses_previous_slice_as_baseline(
    slice_world, fake_llm, monkeypatch
):
    """The next slice's word_range_start matches the previous slice's
    word_range_end — so reviewers see a contiguous timeline."""
    import asyncio

    from database.database import SessionLocal
    from services import summary_slices as ss

    monkeypatch.setattr(ss, "ROOM_SLICE_TRIGGER_WORDS", 10)

    db = SessionLocal()
    try:
        # First chunk of 20 words.
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple=" ".join(f"w{i}" for i in range(20)),
            room_id=slice_world["room_a_id"],
        )
        first = asyncio.run(
            ss.maybe_auto_trigger_slice(db, session, trigger_words=10)
        )
        assert first is not None
        assert first["word_range_end"] == 20
        db.commit()

        # Grow the transcript and fire again.
        session.transcript_simple = " ".join(f"w{i}" for i in range(40))
        from sqlalchemy.orm.attributes import flag_modified
        db.add(session)
        db.commit()
        db.refresh(session)

        second = asyncio.run(
            ss.maybe_auto_trigger_slice(db, session, trigger_words=10)
        )
        assert second is not None
        assert second["word_range_start"] == 20
        assert second["word_range_end"] == 40
        db.commit()

        db.refresh(session)
        all_slices = (session.processing_metadata or {}).get("summary_slices") or []
        assert len(all_slices) == 2
    finally:
        db.close()


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def test_get_summary_slices_empty_for_legacy_session(client, slice_world):
    """GET returns an empty list (not 500) for a session that never had
    any slices written."""
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple="some transcript content",
            room_id=slice_world["room_a_id"],
        )
        session_pub_id = session.session_id
    finally:
        db.close()

    resp = client.get(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_a"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == session_pub_id
    assert body["is_room_session"] is True
    assert body["slices"] == []
    assert body["trigger_words"] >= 1
    assert body["max_per_session"] >= 1


def test_get_summary_slices_returns_persisted_slices_sorted(client, slice_world):
    """GET returns the persisted slice list sorted oldest-first."""
    from datetime import datetime, timedelta, timezone

    from database.database import SessionLocal

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple="hi",
            room_id=slice_world["room_a_id"],
        )
        # Hand-poke three slices with deliberately out-of-order created_at.
        now = datetime.now(timezone.utc)
        slices = [
            {
                "id": "s-c",
                "text": "third",
                "word_count": 30,
                "word_range_start": 20,
                "word_range_end": 30,
                "created_at": (now + timedelta(minutes=5)).isoformat(),
                "triggered_by": "manual",
            },
            {
                "id": "s-a",
                "text": "first",
                "word_count": 10,
                "word_range_start": 0,
                "word_range_end": 10,
                "created_at": now.isoformat(),
                "triggered_by": "room-recorder",
            },
            {
                "id": "s-b",
                "text": "second",
                "word_count": 20,
                "word_range_start": 10,
                "word_range_end": 20,
                "created_at": (now + timedelta(minutes=2)).isoformat(),
                "triggered_by": "room-recorder",
            },
        ]
        session.processing_metadata = {"summary_slices": slices}
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(session, "processing_metadata")
        db.commit()
        session_pub_id = session.session_id
    finally:
        db.close()

    resp = client.get(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_a"],
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()["slices"]
    assert [r["id"] for r in rows] == ["s-a", "s-b", "s-c"], (
        f"Expected oldest-first ordering, got {[r['id'] for r in rows]}"
    )
    # Triggers preserved through the serializer.
    assert rows[0]["triggered_by"] == "room-recorder"
    assert rows[2]["triggered_by"] == "manual"


def test_post_summary_slice_persists_and_returns_slice(
    client, slice_world, fake_llm
):
    """POST creates a slice, persists it on the session, and returns
    the serialized shape."""
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple=" ".join(f"w{i}" for i in range(50)),
            room_id=slice_world["room_a_id"],
        )
        session_pub_id = session.session_id
    finally:
        db.close()

    resp = client.post(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_a"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == session_pub_id
    assert "ROOMSUMMARY" in body["slice"]["text"]
    assert body["slice"]["triggered_by"] == "manual"
    assert body["slice"]["word_range_end"] == 50

    # GET should now show the slice.
    follow = client.get(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_a"],
    )
    assert follow.status_code == 200
    follow_body = follow.json()
    assert len(follow_body["slices"]) == 1
    assert follow_body["slices"][0]["id"] == body["slice"]["id"]


def test_post_summary_slice_empty_transcript_returns_422(client, slice_world, fake_llm):
    """POST against a session that hasn't transcribed anything yet 422s
    rather than rolling an empty slice."""
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple="",
            room_id=slice_world["room_a_id"],
        )
        session_pub_id = session.session_id
    finally:
        db.close()

    resp = client.post(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_a"],
    )
    assert resp.status_code == 422


def test_post_summary_slice_no_new_words_returns_422(client, slice_world, fake_llm):
    """A second POST with no new transcript content should not duplicate
    the previous slice — return 422 so the UI can show a hint."""
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple=" ".join(f"w{i}" for i in range(15)),
            room_id=slice_world["room_a_id"],
        )
        session_pub_id = session.session_id
    finally:
        db.close()

    first = client.post(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_a"],
    )
    assert first.status_code == 200, first.text

    second = client.post(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_a"],
    )
    assert second.status_code == 422


# ---------------------------------------------------------------------------
# Cross-org isolation
# ---------------------------------------------------------------------------


def test_cross_org_get_returns_404(client, slice_world):
    """Org B asking for org A's session id must get 404, not the slice list."""
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple="alpha private content",
            room_id=slice_world["room_a_id"],
        )
        session_pub_id = session.session_id
    finally:
        db.close()

    resp = client.get(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_b"],
    )
    assert resp.status_code == 404


def test_cross_org_post_returns_404(client, slice_world, fake_llm):
    """Org B trying to POST a slice against org A's session id must 404."""
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        session = _seed_session(
            db,
            organization_id=slice_world["org_a_id"],
            user_id=slice_world["user_a_id"],
            transcript_simple=" ".join(f"w{i}" for i in range(40)),
            room_id=slice_world["room_a_id"],
        )
        session_pub_id = session.session_id
    finally:
        db.close()

    resp = client.post(
        f"/api/recordings/sessions/{session_pub_id}/summary-slices",
        headers=slice_world["headers_b"],
    )
    assert resp.status_code == 404
