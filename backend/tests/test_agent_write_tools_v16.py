"""Tests for v1.6 agent write tools: delete_session (typed-confirm friction)
+ start_recording + stop_recording. Reuses the helpers + fake-redis pattern
from test_agent_actions.py.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from tests.test_agent_actions import (
    _models,
    _seed_user_org,
    _seed_session,
    _set_fake_redis,
)


def _set_status(session_id: int, status: str, *, started_at=None) -> None:
    _, _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        row = db.get(RecordingSession, session_id)
        row.status = status
        if started_at is not None:
            row.started_at = started_at
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# delete_session — typed-confirmation friction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_session_typed_confirmation_positive(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="v16-del-pos", username="v16_del_pos")
    session = _seed_session(org_id=org.id, user_id=user.id, title="To be deleted")

    from services.agent_actions import propose_action, confirm_action

    db = SessionLocal()
    try:
        env = await propose_action(
            db=db, user=user, org_id=org.id,
            action="delete_session",
            payload={"session_id": str(session.id)},
        )
        required = env.get("required_typed_confirmation")
        assert required == f"delete-{session.id}"
        result = await confirm_action(
            db=db, user=user, org_id=org.id,
            confirmation_token=env["confirmation_token"],
            typed_confirmation=required,
        )
        assert result["status"] == "applied"
        assert result["result"]["deleted"] is True
        assert db.get(RecordingSession, session.id) is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_session_typed_confirmation_missing_is_rejected(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="v16-del-miss", username="v16_del_miss")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Should survive")

    from services.agent_actions import propose_action, confirm_action

    db = SessionLocal()
    try:
        env = await propose_action(
            db=db, user=user, org_id=org.id,
            action="delete_session",
            payload={"session_id": str(session.id)},
        )
        with pytest.raises(HTTPException) as exc:
            await confirm_action(
                db=db, user=user, org_id=org.id,
                confirmation_token=env["confirmation_token"],
                # no typed_confirmation -> must 409
            )
        assert exc.value.status_code == 409
        assert "Typed confirmation" in str(exc.value.detail)
        assert db.get(RecordingSession, session.id) is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_session_typed_confirmation_wrong_value_is_rejected(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="v16-del-wrong", username="v16_del_wrong")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Should survive too")

    from services.agent_actions import propose_action, confirm_action

    db = SessionLocal()
    try:
        env = await propose_action(
            db=db, user=user, org_id=org.id,
            action="delete_session",
            payload={"session_id": str(session.id)},
        )
        with pytest.raises(HTTPException) as exc:
            await confirm_action(
                db=db, user=user, org_id=org.id,
                confirmation_token=env["confirmation_token"],
                typed_confirmation="yes",
            )
        assert exc.value.status_code == 409
        assert db.get(RecordingSession, session.id) is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_session_refused_while_recording(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    org, user = _seed_user_org(slug="v16-del-rec", username="v16_del_rec")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Mid-recording")
    _set_status(session.id, "recording")

    from services.agent_actions import propose_action

    _, _, _, _, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            await propose_action(
                db=db, user=user, org_id=org.id,
                action="delete_session",
                payload={"session_id": str(session.id)},
            )
        assert exc.value.status_code == 409
    finally:
        db.close()


# ---------------------------------------------------------------------------
# start_recording / stop_recording
# ---------------------------------------------------------------------------

class _FakeAudioService:
    def __init__(self):
        self.started: list[tuple[str, str | None]] = []
        self.stopped: list[str] = []

    def start_recording(self, session_id, device_id=None, output_dir=None):
        self.started.append((session_id, device_id))
        return True, f"/tmp/{session_id}.wav"

    def stop_recording(self, session_id):
        self.stopped.append(session_id)
        return True, f"/tmp/{session_id}.wav"


def _patch_audio(monkeypatch):
    fake = _FakeAudioService()
    import services.working_audio_service as was
    monkeypatch.setattr(was, "audio_service", fake)
    return fake


@pytest.mark.asyncio
async def test_start_recording_round_trip(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    fake_audio = _patch_audio(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="v16-start", username="v16_start")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Start me")
    _set_status(session.id, "pending")

    from services.agent_actions import propose_action, confirm_action

    db = SessionLocal()
    try:
        env = await propose_action(
            db=db, user=user, org_id=org.id,
            action="start_recording",
            payload={"session_id": str(session.id)},
        )
        assert env["status"] == "needs_confirmation"
        result = await confirm_action(
            db=db, user=user, org_id=org.id,
            confirmation_token=env["confirmation_token"],
        )
        assert result["status"] == "applied"
        assert result["result"]["status"] == "recording"
        db.expire_all()
        row = db.get(RecordingSession, session.id)
        assert row.status == "recording"
        assert row.started_at is not None
        assert fake_audio.started, "audio_service.start_recording was not called"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_start_recording_refused_when_already_recording(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _patch_audio(monkeypatch)
    org, user = _seed_user_org(slug="v16-start-busy", username="v16_start_busy")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Already going")
    _set_status(session.id, "recording")

    from services.agent_actions import propose_action

    _, _, _, _, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            await propose_action(
                db=db, user=user, org_id=org.id,
                action="start_recording",
                payload={"session_id": str(session.id)},
            )
        assert exc.value.status_code == 409
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stop_recording_round_trip(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    fake_audio = _patch_audio(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="v16-stop", username="v16_stop")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Stop me")
    _set_status(session.id, "recording", started_at=datetime.now(timezone.utc))

    from services.agent_actions import propose_action, confirm_action

    db = SessionLocal()
    try:
        env = await propose_action(
            db=db, user=user, org_id=org.id,
            action="stop_recording",
            payload={"session_id": str(session.id)},
        )
        result = await confirm_action(
            db=db, user=user, org_id=org.id,
            confirmation_token=env["confirmation_token"],
        )
        assert result["status"] == "applied"
        assert result["result"]["status"] == "processing"
        db.expire_all()
        row = db.get(RecordingSession, session.id)
        assert row.status == "processing"
        assert row.ended_at is not None
        assert fake_audio.stopped, "audio_service.stop_recording was not called"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stop_recording_refused_when_not_recording(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _patch_audio(monkeypatch)
    org, user = _seed_user_org(slug="v16-stop-notrec", username="v16_stop_notrec")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Not recording")
    _set_status(session.id, "pending")

    from services.agent_actions import propose_action

    _, _, _, _, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            await propose_action(
                db=db, user=user, org_id=org.id,
                action="stop_recording",
                payload={"session_id": str(session.id)},
            )
        assert exc.value.status_code == 409
    finally:
        db.close()
