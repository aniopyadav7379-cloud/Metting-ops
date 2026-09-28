"""v3.18.1 authorization-hardening tests (Batch 1c).

Covers the three S0 fixes threaded through this patch:

  1. ``live_transcription`` admin check used ``current_user.get(...)`` on a
     SQLAlchemy ``User`` ORM object — a dict method that raises
     ``AttributeError`` → 500. A non-superuser must now get a clean 403.
  2. ``AlwaysOnRecorder`` created ``RecordingSession`` rows with no
     ``organization_id`` / ``user_id``. Both must now be threaded through
     ``attach_owner(...)``; an unowned recorder must refuse to insert a row.
  3. ``websocket_remote_audio`` looked sessions up by UUID with no org check,
     letting any valid-JWT user append audio cross-org. The lookup is now
     org-scoped and tier-gated (``canonical_reprocess``); cross-org and
     free-tier connections close 4403.

Reuses the seeding + login patterns from ``test_free_tier_enforcement.py``
and the WS-client patterns from ``test_streaming_tier_gate.py``.
"""
from __future__ import annotations

import asyncio
import uuid as _uuid

import pytest
from starlette.websockets import WebSocketDisconnect

from auth.utils import create_access_token, get_password_hash


# ---------------------------------------------------------------------------
# Shared seeding helpers (mirrors test_free_tier_enforcement.py)
# ---------------------------------------------------------------------------


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession
    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _login_headers(client, username, password, org_slug=None):
    resp = client.post(
        "/api/auth/login", data={"username": username, "password": password}
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _seed_user_and_org(slug, username, tier, is_superuser=False):
    Organization, User, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(
                name=slug.replace("-", " ").title(), slug=slug, is_active=True
            )
            db.add(org); db.commit(); db.refresh(org)
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                hashed_password=get_password_hash("admin123"),
                is_active=True, is_verified=True, is_superuser=is_superuser,
            )
            db.add(user); db.commit(); db.refresh(user)
        user.tier = tier
        user.is_superuser = is_superuser
        db.commit()
        mem = (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.organization_id == org.id,
            )
            .first()
        )
        if not mem:
            db.add(UserOrganization(
                user_id=user.id, organization_id=org.id, role="admin"
            ))
            db.commit()
        return org.id, org.slug, user.id
    finally:
        db.close()


def _create_session(org_id, user_id, status="recording"):
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid, name=f"authz-{sid[:8]}", title=f"authz-{sid[:8]}",
            meeting_type="companion", mode="upload", status=status,
            duration=0.0, user_id=user_id, organization_id=org_id,
            source_type="companion_app",
        )
        db.add(s); db.commit(); db.refresh(s)
        return s.id, s.session_id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Change 1: live_transcription admin check — 403 not 500
# ---------------------------------------------------------------------------


def test_live_transcription_superuser_check_returns_403_not_500(client):
    """Non-superuser hitting an admin-only live-transcription endpoint gets a
    clean 403. Before the fix, ``current_user.get("is_superuser")`` raised
    AttributeError on the ORM User → 500."""
    _, org_slug, _ = _seed_user_and_org(
        "authz-lt-user", "authz_lt_user", "pro", is_superuser=False
    )
    headers = _login_headers(client, "authz_lt_user", "admin123", org_slug)

    resp = client.post("/api/live-transcription/start", headers=headers)
    assert resp.status_code == 403, resp.text
    assert resp.status_code != 500
    assert "superuser" in resp.text.lower()


# ---------------------------------------------------------------------------
# Change 2: always-on org/user injection
# ---------------------------------------------------------------------------


def test_always_on_session_has_org_and_user(monkeypatch):
    """A recorder with an attached owner stamps organization_id + user_id on
    the RecordingSession it creates."""
    from services.always_on_recorder import AlwaysOnRecorder
    import services.working_audio_service as was
    import services.live_recording_transcription as lrt

    org_id, _, user_id = _seed_user_and_org(
        "authz-ao-owned", "authz_ao_owned", "enterprise"
    )

    # Stub out the audio + transcription side effects (no ffmpeg in tests).
    # The DB insert happens before these are called, but we need them to not
    # blow up so the success path runs to completion.
    monkeypatch.setattr(
        was.audio_service, "start_recording",
        lambda *a, **k: (True, "/tmp/authz_mock.wav"),
    )

    async def _noop_start_monitoring(*a, **k):
        return None

    monkeypatch.setattr(
        lrt.live_recording_transcription, "start_monitoring", _noop_start_monitoring
    )

    rec = AlwaysOnRecorder()
    rec.attach_owner(org_id, user_id)
    asyncio.run(rec._start_new_meeting())

    assert rec.current_session_id is not None

    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        row = (
            db.query(RecordingSession)
            .filter(RecordingSession.session_id == rec.current_session_id)
            .first()
        )
        assert row is not None, "session row should have been created"
        assert row.organization_id == org_id
        assert row.user_id == user_id
    finally:
        db.close()


def test_always_on_skips_session_without_owner(caplog):
    """A recorder with no owner attached refuses to insert a session and logs
    a warning (no orphaned/unowned row)."""
    import logging
    from services.always_on_recorder import AlwaysOnRecorder

    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        before = db.query(RecordingSession).count()
    finally:
        db.close()

    rec = AlwaysOnRecorder()  # no attach_owner → organization_id/user_id None
    assert rec.organization_id is None and rec.user_id is None

    with caplog.at_level(logging.WARNING):
        asyncio.run(rec._start_new_meeting())

    assert rec.current_session_id is None, "no session should be tracked"

    db = SessionLocal()
    try:
        after = db.query(RecordingSession).count()
    finally:
        db.close()

    assert after == before, "no RecordingSession row should be inserted"
    assert any(
        "no owner attached" in r.message.lower()
        or "skipping session" in r.message.lower()
        for r in caplog.records
    ), f"expected an owner-missing warning, got: {[r.message for r in caplog.records]}"


# ---------------------------------------------------------------------------
# Change 3: websocket_remote_audio org/tier authorization
# ---------------------------------------------------------------------------


def test_websocket_remote_audio_cross_org_rejected(client):
    """A user whose JWT belongs to org A cannot append audio to a session that
    lives in org B. The org-scoped lookup misses → close 4403."""
    # Org A: the attacker (paid tier so the tier gate passes and we actually
    # exercise the org-scope check).
    _, _, attacker_id = _seed_user_and_org(
        "authz-orgA", "authz_attacker", "enterprise"
    )
    # Org B: the victim's org + a session owned by org B.
    org_b_id, _, victim_id = _seed_user_and_org(
        "authz-orgB", "authz_victim", "enterprise"
    )
    _, victim_session = _create_session(org_b_id, victim_id)

    token = create_access_token({"sub": str(attacker_id)})
    url = f"/ws/remote-audio/{victim_session}?token={token}"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(url) as ws:
            # Server accepted, then sends an error frame + close 4403. Drain
            # until the close surfaces as a disconnect.
            ws.receive_json()
            ws.receive_json()

    assert exc_info.value.code == 4403, exc_info.value.code


def test_websocket_remote_audio_free_tier_rejected(client):
    """A free-tier user is rejected before any audio is accepted. The tier
    gate fires ahead of the session lookup → close 4403."""
    org_id, _, user_id = _seed_user_and_org(
        "authz-free", "authz_free", "free"
    )
    # A real, in-org session so we know the rejection is the tier gate, not
    # the org-scope guard.
    _, session_id = _create_session(org_id, user_id)

    token = create_access_token({"sub": str(user_id)})
    url = f"/ws/remote-audio/{session_id}?token={token}"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(url) as ws:
            # Gate closes before accept; the connect/receive raises.
            ws.receive_json()

    assert exc_info.value.code == 4403, exc_info.value.code


def test_websocket_remote_audio_unauthenticated_rejected(client):
    """Regression guard: no token at all still closes 4001 before accept
    (the v3.18.1 changes must not move the unauth reject behind accept)."""
    url = "/ws/remote-audio/whatever-session"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(url) as ws:
            ws.receive_text()

    assert exc_info.value.code == 4001, exc_info.value.code
