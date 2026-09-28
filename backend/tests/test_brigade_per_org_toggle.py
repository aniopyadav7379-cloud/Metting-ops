"""Tests for v3.19.0 per-org integration toggle wiring through brigade_writer.

Hard contract: when Organization.integrations.brigade.enabled == False,
the brigade writer must no-op for that org regardless of env-var creds.
This is the "no cross-org leakage" guarantee — an opted-out customer
must not accidentally have their data dumped into a globally-shared
Brigade because the operator forgot to clear BRIGADE_API_KEY.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import pytest


@dataclass
class _Recorded:
    kind: str
    payload: dict[str, Any]


class _RecordingBrigadeClient:
    """Drop-in BrigadeClient stub that records calls instead of HTTP."""

    def __init__(self) -> None:
        from services.brigade_client import BrigadeWriteResult

        self.calls: list[_Recorded] = []
        self._BrigadeWriteResult = BrigadeWriteResult
        self.is_live = True

    async def upsert_entity(self, **kw) -> Any:
        self.calls.append(_Recorded(kind="entity", payload=kw))
        return self._BrigadeWriteResult(ok=True, mode="live")

    async def upsert_edge(self, **kw) -> Any:
        self.calls.append(_Recorded(kind="edge", payload=kw))
        return self._BrigadeWriteResult(ok=True, mode="live")

    async def aclose(self) -> None:
        return None


def _models():
    from auth.models import Organization, User
    from database.database import SessionLocal
    from database.models import RecordingSession

    return Organization, User, SessionLocal, RecordingSession


def _seed_session(integrations_payload: dict[str, Any]) -> tuple[int, int]:
    """Create one org with the given integrations dict + one completed
    session. Returns (org_id, session_id)."""
    Organization, User, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        org = Organization(
            name=f"PerOrgToggle {suffix}",
            slug=f"pot-{suffix}",
            is_active=True,
            integrations=integrations_payload,
        )
        db.add(org)
        db.commit()
        db.refresh(org)

        user = db.query(User).filter(User.username == "admin").first()
        session = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="per-org toggle test",
            title="per-org toggle test",
            status="completed",
            organization_id=org.id,
            user_id=user.id if user else None,
            transcript_simple="hi",
            transcript="hi",
            duration=10.0,
            participants=[],
            final_summary={"bullets": [], "decisions": []},
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return org.id, session.id
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _clear_brigade_env(monkeypatch):
    monkeypatch.delenv("BRIGADE_API_KEY", raising=False)
    monkeypatch.delenv("BRIGADE_ADMIN_KEY", raising=False)
    monkeypatch.delenv("BRIGADE_TENANCY_MODE", raising=False)
    yield


def test_disabled_org_is_noop_even_with_env_api_key(client, monkeypatch):
    """The headline guarantee. With BRIGADE_API_KEY set in env (the
    legacy global-creds posture) AND the org's integrations.brigade
    flipped OFF, the writer must NOT write to Brigade. It returns
    ok=True/mode=noop and the stub client sees ZERO calls.
    """
    from database.database import SessionLocal
    from services.brigade_writer import write_meeting_to_brigade

    monkeypatch.setenv("BRIGADE_API_KEY", "leaked-global-key")
    monkeypatch.setenv("BRIGADE_API_BASE_URL", "http://brigade.local")

    org_id, session_id = _seed_session(
        {
            "brigade": {
                "enabled": False,
                "api_base_url": "",
                "api_key_encrypted": "",
            }
        }
    )

    stub = _RecordingBrigadeClient()
    db = SessionLocal()
    try:
        result = asyncio.run(
            write_meeting_to_brigade(session_id, db, client=stub)
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "noop"
    # CRITICAL — no upserts hit the (stub) client even though env said "go".
    assert stub.calls == []


def test_no_integrations_block_falls_through_to_env(client, monkeypatch):
    """Backwards compat: an org with NO integrations block at all
    (existing customers pre-v3.19) keeps using the env-var creds. The
    writer fires its normal path."""
    from database.database import SessionLocal
    from services.brigade_writer import write_meeting_to_brigade

    # No env key — we just want to confirm the writer reaches the
    # client. With no env key the stub still gets called (we passed it
    # explicitly), proving we didn't short-circuit at the toggle.
    org_id, session_id = _seed_session({})

    stub = _RecordingBrigadeClient()
    db = SessionLocal()
    try:
        result = asyncio.run(
            write_meeting_to_brigade(session_id, db, client=stub)
        )
    finally:
        db.close()

    assert result.ok is True
    # At least the Meeting entity got written.
    entity_calls = [c for c in stub.calls if c.kind == "entity"]
    assert any(
        c.payload.get("entity_type") == "Meeting" for c in entity_calls
    ), "writer should have fired through to the client when no org override exists"


def test_enabled_org_override_uses_org_creds_not_env(client, monkeypatch):
    """When the org's brigade block is enabled with org-specific creds,
    the per-org config takes precedence over env-vars. We can't observe
    the real client construction here (the stub bypasses it), but we
    confirm the writer reaches the call path successfully."""
    from database.database import SessionLocal
    from services.brigade_writer import write_meeting_to_brigade
    from services.providers.crypto import encrypt_api_key

    monkeypatch.setenv("BRIGADE_API_KEY", "stale-env-key")

    org_id, session_id = _seed_session(
        {
            "brigade": {
                "enabled": True,
                "api_base_url": "http://brigade.org-specific.local",
                "api_key_encrypted": encrypt_api_key("org-specific-key"),
                "tenancy_mode": "per_org_graph",
            }
        }
    )

    stub = _RecordingBrigadeClient()
    db = SessionLocal()
    try:
        result = asyncio.run(
            write_meeting_to_brigade(session_id, db, client=stub)
        )
    finally:
        db.close()

    assert result.ok is True
    # The org's tenancy_mode=per_org_graph should derive a per-org graph
    # name on the Meeting node properties.
    meeting = next(
        c for c in stub.calls if c.payload.get("entity_type") == "Meeting"
    )
    assert meeting.payload["properties"]["tenancy_mode"] == "per_org_graph"
    assert (
        meeting.payload["properties"]["graph_name"]
        == f"agent_meeting_ops_org_{org_id}"
    )
