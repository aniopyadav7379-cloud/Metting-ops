"""Tests for POST /api/simple/recording-sessions/{id}/move-org.

Covers:

  - Happy path: admin in source + member in target -> 200, cascade
    counts return non-zero rows, the session row's organization_id
    flips, audio_files/action_items/attachments tag along.
  - Non-admin in source org: 403.
  - Caller not a member of target org: 403.
  - Same-source-and-target -> 400.
  - Cross-org leak: a moved session becomes invisible to the source
    org's members and visible to the target org's members on the
    standard sessions list endpoint.
  - Orphaned speaker links surfaced (speaker_session_link.speaker_id
    that points at a source-org SpeakerProfile).
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import (
        ActionItem,
        AudioFile,
        ChatHistory,
        RecordingSession,
        SessionAttachment,
        Speaker as SpeakerProfileLegacy,  # noqa: F401 — not used here
        SpeakerProfile,
        SpeakerSessionLink,
    )

    return {
        "Organization": Organization,
        "User": User,
        "UserOrganization": UserOrganization,
        "SessionLocal": SessionLocal,
        "RecordingSession": RecordingSession,
        "ActionItem": ActionItem,
        "AudioFile": AudioFile,
        "ChatHistory": ChatHistory,
        "SessionAttachment": SessionAttachment,
        "SpeakerProfile": SpeakerProfile,
        "SpeakerSessionLink": SpeakerSessionLink,
    }


def _seed_user(db, username: str, password: str, email: str, *, is_superuser: bool = False):
    m = _models()
    user = m["User"](
        email=email,
        username=username,
        hashed_password=get_password_hash(password),
        is_active=True,
        is_verified=True,
        is_superuser=is_superuser,
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


def _extract_items(body: Any) -> list:
    """The /recording-sessions endpoint has returned both a flat list AND
    a {recordings: [...]} envelope across the history of the codebase.
    Tolerate both shapes."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("recordings", "sessions", "items"):
            v = body.get(key)
            if isinstance(v, list):
                return v
    return []


def _make_session(db, *, organization_id: int, user_id: int, title: str):
    m = _models()
    sess = m["RecordingSession"](
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        status="completed",
        organization_id=organization_id,
        user_id=user_id,
        transcript_simple="hello world",
        transcript="hello world",
        summary="brief",
        duration=60.0,
        participants=[],
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


@pytest.fixture()
def two_orgs_move(client):
    m = _models()
    db = m["SessionLocal"]()
    suffix = uuid.uuid4().hex[:6]

    try:
        org_a = m["Organization"](
            name=f"Org Alpha {suffix}", slug=f"alpha-{suffix}", is_active=True
        )
        org_b = m["Organization"](
            name=f"Org Bravo {suffix}", slug=f"bravo-{suffix}", is_active=True
        )
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        # Caller is admin in A, member of B (so they can move into B).
        caller = _seed_user(
            db,
            f"caller_{suffix}",
            "Password123",
            f"caller_{suffix}@example.com",
        )
        # Outsider is a member of B only (used to assert B-side
        # visibility AFTER the move).
        outsider_b = _seed_user(
            db,
            f"outb_{suffix}",
            "Password123",
            f"outb_{suffix}@example.com",
        )
        # A-only viewer (no access to B). Used to assert source-side
        # invisibility AFTER the move.
        viewer_a = _seed_user(
            db,
            f"viewer_a_{suffix}",
            "Password123",
            f"viewer_a_{suffix}@example.com",
        )

        db.add_all([
            m["UserOrganization"](
                user_id=caller.id, organization_id=org_a.id, role="admin"
            ),
            m["UserOrganization"](
                user_id=caller.id, organization_id=org_b.id, role="user"
            ),
            m["UserOrganization"](
                user_id=outsider_b.id, organization_id=org_b.id, role="user"
            ),
            m["UserOrganization"](
                user_id=viewer_a.id, organization_id=org_a.id, role="user"
            ),
        ])
        db.commit()

        sess = _make_session(
            db, organization_id=org_a.id, user_id=caller.id, title="Movable"
        )

        # Seed some child rows so the cascade has work to do.
        db.add(
            m["ActionItem"](
                session_id=sess.id,
                organization_id=org_a.id,
                text="Follow up",
                status="todo",
                source="manual",
            )
        )
        db.add(
            m["AudioFile"](
                file_id=str(uuid.uuid4()),
                session_id=sess.session_id,  # NOTE: string id
                organization_id=org_a.id,
                filename="audio.wav",
                file_path="/app/recordings/audio.wav",
                file_size=1234,
                file_format="wav",
            )
        )
        db.add(
            m["ChatHistory"](
                session_key=sess.session_id,
                organization_id=org_a.id,
                role="user",
                content="hi",
            )
        )
        # Speaker profile in source org + a session link referencing it,
        # so we can assert the orphaned-link list comes back populated.
        spk = m["SpeakerProfile"](
            organization_id=org_a.id, display_name="Source Org Speaker"
        )
        db.add(spk)
        db.commit()
        db.refresh(spk)
        db.add(
            m["SpeakerSessionLink"](
                session_id=sess.id,
                organization_id=org_a.id,
                raw_label="SPEAKER_00",
                speaker_id=spk.id,
                source="manual",
                confirmed=True,
            )
        )
        db.commit()

        ctx = {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "org_a_slug": org_a.slug,
            "org_b_slug": org_b.slug,
            "sess_pub": sess.session_id,
            "sess_pk": sess.id,
            "caller_username": caller.username,
            "outsider_b_username": outsider_b.username,
            "viewer_a_username": viewer_a.username,
            "spk_id_source": spk.id,
        }
    finally:
        db.close()

    ctx["headers_a"] = _login(client, ctx["caller_username"], "Password123")
    ctx["headers_a"]["X-MeetingOps-Org"] = ctx["org_a_slug"]
    ctx["headers_b"] = _login(client, ctx["caller_username"], "Password123")
    ctx["headers_b"]["X-MeetingOps-Org"] = ctx["org_b_slug"]
    ctx["headers_viewer_a"] = _login(client, ctx["viewer_a_username"], "Password123")
    ctx["headers_viewer_a"]["X-MeetingOps-Org"] = ctx["org_a_slug"]
    ctx["headers_outsider_b"] = _login(client, ctx["outsider_b_username"], "Password123")
    ctx["headers_outsider_b"]["X-MeetingOps-Org"] = ctx["org_b_slug"]
    return ctx


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_move_session_happy_path(client, two_orgs_move, monkeypatch):
    """admin@A + member@B can move a session A -> B and the cascade
    moves child rows along with it."""
    ctx = two_orgs_move

    # Skip the Qdrant retag — the test stack doesn't run Qdrant.
    # Patch the function to return (True, None) so the response shape
    # is the happy-path one. The cascade SQL still runs and is the
    # interesting assertion.
    from api import session_move_org as mod

    monkeypatch.setattr(
        mod, "_retag_qdrant", lambda **kw: (True, None)
    )

    resp = client.post(
        f"/api/simple/recording-sessions/{ctx['sess_pub']}/move-org",
        headers=ctx["headers_a"],
        json={"target_organization_id": ctx["org_b_id"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["source_organization_id"] == ctx["org_a_id"]
    assert body["target_organization_id"] == ctx["org_b_id"]
    counts = body["moved_counts"]
    assert counts["recording_sessions"] == 1
    assert counts["action_items"] == 1
    assert counts["audio_files"] == 1
    assert counts["chat_history"] == 1
    assert counts["speaker_session_link"] == 1
    # No upload_jobs / tts_jobs / attachments in this fixture, so zeros.
    assert counts["upload_jobs"] == 0
    assert counts["tts_jobs"] == 0
    assert counts["session_attachments"] == 0

    # Orphan list: speaker_session_link.speaker_id still points at a
    # SpeakerProfile in org A.
    assert len(body["orphaned_speaker_links"]) == 1
    o = body["orphaned_speaker_links"][0]
    assert o["raw_label"] == "SPEAKER_00"
    assert o["speaker_display"] == "Source Org Speaker"
    assert o["speaker_id"] == ctx["spk_id_source"]

    # DB-side: the session row's org_id flipped.
    m = _models()
    db = m["SessionLocal"]()
    try:
        sess = db.query(m["RecordingSession"]).filter(
            m["RecordingSession"].id == ctx["sess_pk"]
        ).first()
        assert sess.organization_id == ctx["org_b_id"]

        # Children also flipped:
        assert (
            db.query(m["ActionItem"])
            .filter(m["ActionItem"].session_id == ctx["sess_pk"])
            .first()
            .organization_id
            == ctx["org_b_id"]
        )
        af = db.query(m["AudioFile"]).filter(
            m["AudioFile"].session_id == ctx["sess_pub"]
        ).first()
        assert af.organization_id == ctx["org_b_id"]
        ch = db.query(m["ChatHistory"]).filter(
            m["ChatHistory"].session_key == ctx["sess_pub"]
        ).first()
        assert ch.organization_id == ctx["org_b_id"]
        ssl = db.query(m["SpeakerSessionLink"]).filter(
            m["SpeakerSessionLink"].session_id == ctx["sess_pk"]
        ).first()
        assert ssl.organization_id == ctx["org_b_id"]
    finally:
        db.close()


def test_move_visible_only_to_target_org_after_move(client, two_orgs_move, monkeypatch):
    """After move A -> B: viewers in A don't see the session in their
    list, viewers in B do."""
    ctx = two_orgs_move

    from api import session_move_org as mod

    monkeypatch.setattr(mod, "_retag_qdrant", lambda **kw: (True, None))

    # Pre-move: A viewer sees it.
    resp = client.get(
        "/api/simple/recording-sessions",
        headers=ctx["headers_viewer_a"],
    )
    assert resp.status_code == 200
    body = resp.json()
    items = _extract_items(body)
    found_before = any(
        it.get("id") == ctx["sess_pub"] or it.get("session_id") == ctx["sess_pub"]
        for it in items
    )
    assert found_before, "A-viewer should see the session BEFORE the move"

    # Move.
    resp = client.post(
        f"/api/simple/recording-sessions/{ctx['sess_pub']}/move-org",
        headers=ctx["headers_a"],
        json={"target_organization_id": ctx["org_b_id"]},
    )
    assert resp.status_code == 200, resp.text

    # Post-move: A viewer no longer sees it.
    resp = client.get(
        "/api/simple/recording-sessions",
        headers=ctx["headers_viewer_a"],
    )
    items = _extract_items(resp.json())
    found_after_a = any(
        it.get("id") == ctx["sess_pub"] or it.get("session_id") == ctx["sess_pub"]
        for it in items
    )
    assert not found_after_a, "Session must NOT be visible to source-org viewers after move"

    # Post-move: B viewer sees it.
    resp = client.get(
        "/api/simple/recording-sessions",
        headers=ctx["headers_outsider_b"],
    )
    items = _extract_items(resp.json())
    found_after_b = any(
        it.get("id") == ctx["sess_pub"] or it.get("session_id") == ctx["sess_pub"]
        for it in items
    )
    assert found_after_b, "Session MUST be visible to target-org viewers after move"


# ---------------------------------------------------------------------------
# Auth failure modes
# ---------------------------------------------------------------------------


def test_non_admin_in_source_org_blocked(client, two_orgs_move, monkeypatch):
    """A 'user' role in the source org who is NOT the session creator
    cannot move the session (403)."""
    ctx = two_orgs_move
    # viewer_a is a 'user' in A, never created the session.
    resp = client.post(
        f"/api/simple/recording-sessions/{ctx['sess_pub']}/move-org",
        headers=ctx["headers_viewer_a"],
        json={"target_organization_id": ctx["org_b_id"]},
    )
    assert resp.status_code == 403, resp.text


def test_not_member_of_target_org_blocked(client, two_orgs_move):
    """Caller is admin in source but has no membership in target -> 403."""
    ctx = two_orgs_move

    # Make a third org the caller has no membership in.
    m = _models()
    db = m["SessionLocal"]()
    suffix = uuid.uuid4().hex[:6]
    try:
        org_c = m["Organization"](
            name=f"Org Charlie {suffix}",
            slug=f"charlie-{suffix}",
            is_active=True,
        )
        db.add(org_c)
        db.commit()
        db.refresh(org_c)
        target_org_c_id = org_c.id
    finally:
        db.close()

    resp = client.post(
        f"/api/simple/recording-sessions/{ctx['sess_pub']}/move-org",
        headers=ctx["headers_a"],
        json={"target_organization_id": target_org_c_id},
    )
    assert resp.status_code == 403, resp.text


def test_same_source_and_target_rejected(client, two_orgs_move):
    """No-op move is a 400, not silent success."""
    ctx = two_orgs_move
    resp = client.post(
        f"/api/simple/recording-sessions/{ctx['sess_pub']}/move-org",
        headers=ctx["headers_a"],
        json={"target_organization_id": ctx["org_a_id"]},
    )
    assert resp.status_code == 400, resp.text


def test_target_org_missing_returns_404(client, two_orgs_move):
    ctx = two_orgs_move
    resp = client.post(
        f"/api/simple/recording-sessions/{ctx['sess_pub']}/move-org",
        headers=ctx["headers_a"],
        json={"target_organization_id": 999_999},
    )
    assert resp.status_code == 404, resp.text


def test_session_not_in_source_org_returns_404(client, two_orgs_move):
    """A session that lives in org B is invisible from org A's view —
    even before we get to permission checks, the resolve step 404s."""
    ctx = two_orgs_move

    # Create a B-org session.
    m = _models()
    db = m["SessionLocal"]()
    try:
        sess_b = _make_session(
            db,
            organization_id=ctx["org_b_id"],
            user_id=None,
            title="B-only",
        )
        b_pub = sess_b.session_id
    finally:
        db.close()

    resp = client.post(
        f"/api/simple/recording-sessions/{b_pub}/move-org",
        headers=ctx["headers_a"],
        json={"target_organization_id": ctx["org_b_id"]},
    )
    assert resp.status_code == 404, resp.text
