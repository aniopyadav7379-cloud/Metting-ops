"""Full-snapshot drift detection for agent write tools (v3.18.3).

Previous behavior: ``apply_delete_session`` and ``apply_stop_recording``
only compared the live ``status`` field against the proposal's before
snapshot. Any other captured field (e.g. ``audio_object_key``,
``started_at``) could mutate between propose and confirm without the
applier noticing.

v3.18.3 ships a generic ``_assert_no_drift`` helper that compares every
key in ``before``. These tests pin that behavior.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_agent_actions import (
    _models,
    _seed_session,
    _seed_user_org,
    _set_fake_redis,
)


def _mutate(session_pk: int, **fields) -> None:
    _, _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        row = db.get(RecordingSession, session_pk)
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_session_drift_on_audio_object_key_blocks_confirm(app, monkeypatch):
    """The before-snapshot pins audio_object_key. If a re-upload swaps the
    key between propose and confirm, the applier must refuse so we don't
    purge the wrong blob."""
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="drift-del-aok", username="drift_del_aok")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Drift via aok")

    # Stamp an initial audio_object_key so the before-snapshot captures it.
    _mutate(session.id, audio_object_key="meeting-ops-audio/orig.wav")

    from services.agent_actions import propose_action, confirm_action
    from services.agent_write_tools import DriftError

    db = SessionLocal()
    try:
        env = await propose_action(
            db=db, user=user, org_id=org.id,
            action="delete_session",
            payload={"session_id": str(session.id)},
        )
        required = env.get("required_typed_confirmation")

        # Simulate a concurrent re-upload swapping the canonical audio key.
        _mutate(session.id, audio_object_key="meeting-ops-audio/REPLACED.wav")

        with pytest.raises((DriftError, Exception)) as excinfo:
            await confirm_action(
                db=db, user=user, org_id=org.id,
                confirmation_token=env["confirmation_token"],
                typed_confirmation=required,
            )
        # DriftError is the canonical raise; in HTTP context it surfaces as
        # 409 with the audio_object_key field name in the detail.
        msg = str(getattr(excinfo.value, "detail", excinfo.value))
        assert "audio_object_key" in msg or "drift" in msg.lower() or "Drift" in msg
        # The session must still be present — no DB deletion.
        assert db.get(RecordingSession, session.id) is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stop_recording_drift_on_started_at_blocks_confirm(app, monkeypatch):
    """If started_at changes between propose and confirm, the applier must
    refuse (the user re-started the session before confirming the old stop)."""
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="drift-stop-sa", username="drift_stop_sa")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Drift via started_at")

    # Set status=recording with a fixed started_at so the before-snapshot pins it.
    started_a = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
    _mutate(session.id, status="recording", started_at=started_a)

    from services.agent_actions import propose_action, confirm_action
    from services.agent_write_tools import DriftError

    db = SessionLocal()
    try:
        env = await propose_action(
            db=db, user=user, org_id=org.id,
            action="stop_recording",
            payload={"session_id": str(session.id)},
        )

        # Simulate a restart: status stays recording but started_at moves forward.
        started_b = started_a + timedelta(minutes=5)
        _mutate(session.id, started_at=started_b)

        with pytest.raises((DriftError, Exception)) as excinfo:
            await confirm_action(
                db=db, user=user, org_id=org.id,
                confirmation_token=env["confirmation_token"],
            )
        msg = str(getattr(excinfo.value, "detail", excinfo.value))
        assert "started_at" in msg or "drift" in msg.lower() or "Drift" in msg
        # Session should still be in recording state (not flipped to processing).
        row = db.get(RecordingSession, session.id)
        assert row.status == "recording"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delete_session_no_drift_succeeds(app, monkeypatch):
    """Positive case: nothing mutates between propose and confirm, deletion
    goes through. This guards against an over-eager drift check breaking
    the happy path."""
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="drift-del-ok", username="drift_del_ok")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Drift happy path")
    _mutate(session.id, audio_object_key="meeting-ops-audio/stable.wav")

    from services.agent_actions import propose_action, confirm_action

    db = SessionLocal()
    try:
        env = await propose_action(
            db=db, user=user, org_id=org.id,
            action="delete_session",
            payload={"session_id": str(session.id)},
        )
        required = env.get("required_typed_confirmation")
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
