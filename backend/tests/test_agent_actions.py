from __future__ import annotations

import asyncio
import json
import sys
import types
import uuid
from pathlib import Path

try:
    import prometheus_client  # noqa: F401
except Exception:  # pragma: no cover - local test env fallback
    fake_prom = types.ModuleType("prometheus_client")

    def make_asgi_app():
        async def app(scope, receive, send):
            if scope["type"] != "http":
                return
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            })
            await send({"type": "http.response.body", "body": b""})

        return app

    fake_prom.make_asgi_app = make_asgi_app
    sys.modules["prometheus_client"] = fake_prom

import pytest
from fastapi import BackgroundTasks, HTTPException

from auth.utils import get_password_hash


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        return True

    async def get(self, key: str):
        return self.store.get(key)

    async def execute_command(self, command: str, key: str):
        if command != "GETDEL":
            raise NotImplementedError(command)
        return self.store.pop(key, None)

    async def delete(self, key: str):
        self.store.pop(key, None)
        return 1


def _models():
    from auth.models import AuditLog, Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession
    return AuditLog, Organization, User, UserOrganization, SessionLocal, RecordingSession


def _seed_user_org(*, slug: str, username: str, tier: str = "pro", is_superuser: bool = False):
    AuditLog, Organization, User, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)

        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                hashed_password=get_password_hash("admin123"),
                is_active=True,
                is_verified=True,
                is_superuser=is_superuser,
                tier=tier,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        user.tier = tier
        user.is_superuser = is_superuser
        db.commit()

        membership = (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.organization_id == org.id,
            )
            .first()
        )
        if not membership:
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
            db.commit()

        db.refresh(org)
        db.refresh(user)
        return org, user
    finally:
        db.close()


def _seed_session(
    *,
    org_id: int,
    user_id: int,
    title: str = "Original Title",
    tags: list[str] | None = None,
    audio: bool = False,
):
    _, _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        session = RecordingSession(
            session_id=str(uuid.uuid4()),
            title=title,
            name=title,
            status="completed",
            user_id=user_id,
            organization_id=org_id,
            tags=tags or [],
            title_user_set=True,
            audio_file=str(Path("/tmp") / f"{uuid.uuid4()}.wav") if audio else None,
            processing_metadata={"reprocess_status": None},
            final_summary={
                "executive": "We discussed follow-up items.",
                "decisions": ["Proceed with the plan"],
                "actions": ["Send the recap"],
            },
        )
        if audio:
            audio_path = Path(session.audio_file)
            audio_path.write_bytes(b"fake")
        db.add(session)
        db.commit()
        db.refresh(session)
        return session
    finally:
        db.close()


def _login_headers(client, username: str, password: str = "admin123", org_slug: str | None = None):
    resp = client.post("/api/auth/login", data={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _set_fake_redis(monkeypatch):
    import services.agent_actions as agent_actions

    fake = FakeRedis()
    monkeypatch.setattr(agent_actions, "_REDIS_CLIENT", fake)
    return fake


def test_http_propose_confirm_round_trip(client, monkeypatch):
    fake = _set_fake_redis(monkeypatch)
    AuditLog, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="agent-write-http", username="agent_http", tier="pro")
    session = _seed_session(org_id=org.id, user_id=user.id, title="HTTP Round Trip")

    headers = _login_headers(client, "agent_http", org_slug=org.slug)

    propose_resp = client.post(
        "/api/agent-actions/propose",
        headers=headers,
        json={
            "action": "rename_session",
            "payload": {
                "session_id": str(session.id),
                "title": "HTTP Round Trip (final)",
            },
        },
    )
    assert propose_resp.status_code == 200, propose_resp.text
    proposal = propose_resp.json()
    assert proposal["status"] == "needs_confirmation"
    assert proposal["action"] == "rename_session"
    assert proposal["confirmation_token"].startswith("phc_v1_")
    assert fake.store

    confirm_resp = client.post(
        "/api/agent-actions/confirm",
        headers=headers,
        json={"confirmation_token": proposal["confirmation_token"]},
    )
    assert confirm_resp.status_code == 200, confirm_resp.text
    result = confirm_resp.json()
    assert result["status"] == "applied"
    assert result["result"]["title"] == "HTTP Round Trip (final)"

    db = SessionLocal()
    try:
        session_row = db.query(RecordingSession).filter(RecordingSession.id == session.id).first()
        assert session_row.title == "HTTP Round Trip (final)"
        audit_rows = (
            db.query(AuditLog)
            .filter(
                AuditLog.organization_id == org.id,
                AuditLog.resource_id == proposal["proposal_id"],
            )
            .order_by(AuditLog.id.asc())
            .all()
        )
        assert [row.action for row in audit_rows] == [
            "agent_action_proposed",
            "agent_action_confirmed",
        ]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_propose_confirm_rename_round_trip_and_audit(app, monkeypatch):
    fake = _set_fake_redis(monkeypatch)
    AuditLog, _, User, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="agent-write-roundtrip", username="agent_writer", tier="pro")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Transcription System Review")

    from services.agent_actions import propose_action, confirm_action

    db = SessionLocal()
    try:
        proposal = await propose_action(
            db=db,
            user=user,
            org_id=org.id,
            action="rename_session",
            payload={"session_id": str(session.id), "title": "Transcription System Review (final)"},
        )
        assert proposal["status"] == "needs_confirmation"
        assert proposal["action"] == "rename_session"
        assert proposal["confirmation_token"].startswith("phc_v1_")
        assert fake.store

        result = await confirm_action(
            db=db,
            user=user,
            org_id=org.id,
            confirmation_token=proposal["confirmation_token"],
            background_tasks=BackgroundTasks(),
        )
        session_row = db.query(RecordingSession).filter(RecordingSession.id == session.id).first()
        assert result["status"] == "applied"
        assert session_row.title == "Transcription System Review (final)"
        assert session_row.name == "Transcription System Review (final)"
        assert session_row.title_user_set is True

        rows = (
            db.query(AuditLog)
            .filter(
                AuditLog.organization_id == org.id,
                AuditLog.resource_id == proposal["proposal_id"],
            )
            .order_by(AuditLog.id.asc())
            .all()
        )
        assert [row.action for row in rows] == [
            "agent_action_proposed",
            "agent_action_confirmed",
        ]
        assert rows[0].details["proposal_id"] == proposal["proposal_id"]
        assert rows[1].details["result"]["title"] == "Transcription System Review (final)"
        assert rows[1].details["token_consumed"] is True
    finally:
        db.close()


@pytest.mark.asyncio
async def test_replayed_token_is_rejected(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="agent-write-replay", username="agent_replay", tier="pro")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Replay Test")

    from services.agent_actions import confirm_action, propose_action

    db = SessionLocal()
    try:
        proposal = await propose_action(
            db=db,
            user=user,
            org_id=org.id,
            action="rename_session",
            payload={"session_id": str(session.id), "title": "Replay Test (new)"},
        )
        await confirm_action(
            db=db,
            user=user,
            org_id=org.id,
            confirmation_token=proposal["confirmation_token"],
        )
        with pytest.raises(HTTPException) as exc:
            await confirm_action(
                db=db,
                user=user,
                org_id=org.id,
                confirmation_token=proposal["confirmation_token"],
            )
        assert exc.value.status_code == 404
    finally:
        db.close()


@pytest.mark.asyncio
async def test_expired_token_is_rejected(app, monkeypatch):
    fake = _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="agent-write-expired", username="agent_expired", tier="pro")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Expired Token Test")

    from services.agent_actions import confirm_action, propose_action
    from services.agent_actions import _proposal_key

    db = SessionLocal()
    try:
        proposal = await propose_action(
            db=db,
            user=user,
            org_id=org.id,
            action="rename_session",
            payload={"session_id": str(session.id), "title": "Expired Token Test (new)"},
        )
        stored = json.loads(fake.store[_proposal_key(proposal["proposal_id"])])
        stored["expires_at"] = "2000-01-01T00:00:00+00:00"
        fake.store[_proposal_key(proposal["proposal_id"])] = json.dumps(stored)
        with pytest.raises(HTTPException) as exc:
            await confirm_action(
                db=db,
                user=user,
                org_id=org.id,
                confirmation_token=proposal["confirmation_token"],
            )
        assert exc.value.status_code == 410
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cross_org_confirmation_is_rejected(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org1, user = _seed_user_org(slug="agent-write-org1", username="agent_cross_org", tier="pro")
    org2, _ = _seed_user_org(slug="agent-write-org2", username="agent_cross_org_other", tier="pro")
    # same user must belong to org2 so the request can authenticate there
    db = SessionLocal()
    try:
        from auth.models import UserOrganization

        if not db.query(UserOrganization).filter(
            UserOrganization.user_id == user.id,
            UserOrganization.organization_id == org2.id,
        ).first():
            db.add(UserOrganization(user_id=user.id, organization_id=org2.id, role="admin"))
            db.commit()
    finally:
        db.close()

    session = _seed_session(org_id=org1.id, user_id=user.id, title="Cross Org Test")

    from services.agent_actions import confirm_action, propose_action

    db = SessionLocal()
    try:
        proposal = await propose_action(
            db=db,
            user=user,
            org_id=org1.id,
            action="rename_session",
            payload={"session_id": str(session.id), "title": "Cross Org Test (new)"},
        )
        with pytest.raises(HTTPException) as exc:
            await confirm_action(
                db=db,
                user=user,
                org_id=org2.id,
                confirmation_token=proposal["confirmation_token"],
            )
        assert exc.value.status_code == 403
    finally:
        db.close()


@pytest.mark.asyncio
async def test_tier_gated_action_is_rejected(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="agent-write-free", username="agent_free", tier="free")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Tier Gate Test", audio=True)

    from services.agent_actions import propose_action

    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            await propose_action(
                db=db,
                user=user,
                org_id=org.id,
                action="trigger_reprocess",
                payload={"session_id": str(session.id)},
            )
        assert exc.value.status_code == 403
    finally:
        db.close()


@pytest.mark.asyncio
async def test_payload_tamper_is_rejected(app, monkeypatch):
    fake = _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="agent-write-tamper", username="agent_tamper", tier="pro")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Tamper Test")

    from services.agent_actions import confirm_action, propose_action, _proposal_key

    db = SessionLocal()
    try:
        proposal = await propose_action(
            db=db,
            user=user,
            org_id=org.id,
            action="rename_session",
            payload={"session_id": str(session.id), "title": "Tamper Test (new)"},
        )
        stored = json.loads(fake.store[_proposal_key(proposal["proposal_id"])])
        stored["payload"]["title"] = "Tampered Title"
        fake.store[_proposal_key(proposal["proposal_id"])] = json.dumps(stored)
        with pytest.raises(HTTPException) as exc:
            await confirm_action(
                db=db,
                user=user,
                org_id=org.id,
                confirmation_token=proposal["confirmation_token"],
            )
        assert exc.value.status_code == 409
    finally:
        db.close()


@pytest.mark.asyncio
async def test_state_drift_is_rejected(app, monkeypatch):
    _set_fake_redis(monkeypatch)
    _, _, _, _, SessionLocal, RecordingSession = _models()
    org, user = _seed_user_org(slug="agent-write-drift", username="agent_drift", tier="pro")
    session = _seed_session(org_id=org.id, user_id=user.id, title="Drift Test")

    from services.agent_actions import confirm_action, propose_action

    db = SessionLocal()
    try:
        proposal = await propose_action(
            db=db,
            user=user,
            org_id=org.id,
            action="rename_session",
            payload={"session_id": str(session.id), "title": "Drift Test (new)"},
        )
        session_row = db.query(RecordingSession).filter(RecordingSession.id == session.id).first()
        session_row.title = "Drifted Title"
        session_row.name = "Drifted Title"
        db.commit()
        with pytest.raises(HTTPException) as exc:
            await confirm_action(
                db=db,
                user=user,
                org_id=org.id,
                confirmation_token=proposal["confirmation_token"],
            )
        assert exc.value.status_code == 409
    finally:
        db.close()
