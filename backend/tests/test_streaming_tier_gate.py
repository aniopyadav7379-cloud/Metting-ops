"""Phase B.4: tier-gate tests for `/ws/sessions/{session_id}/live`.

Covers:
  - free-tier user: gets `{"type":"error","reason":"tier_insufficient",...}`
    then close code 4003 with reason "tier_insufficient";
  - pro-tier user: gets `{"type":"ready",...}`, connection stays open;
  - enterprise-tier user: same as pro;
  - superuser with tier='free' but is_superuser=True: bypass works
    (gets ready, not tier-rejected) — verifies `get_user_tier()`'s
    is_superuser override;
  - unauthed connect (no headers, no DB user match): existing 4001
    unauth close code path still works (regression check, makes sure
    the B.4 gate didn't break B.1's auth path).

We monkeypatch ``backend/api/streaming.py``'s module-level
``_resolve_ws_user`` to inject a test User without going through the
oauth2-proxy header dance or the SQLite session machinery. Each test
constructs the User it wants the WS handler to see and verifies the
exact frame + close code the client experiences.

The dataclass User stand-in matches the attribute surface that
``auth/tier.py`` (``is_superuser``, ``tier``, ``email``) actually reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest
from starlette.websockets import WebSocketDisconnect


# ---------------------------------------------------------------------------
# Lightweight User stand-in
# ---------------------------------------------------------------------------
#
# `_resolve_ws_user` returns an object with `.email`, `.is_superuser`, and
# `.tier`. We don't need a real SQLAlchemy row; the tier helpers only read
# attributes.


@dataclass
class _FakeWSUser:
    email: str = "tester@meeting-ops.local"
    tier: Optional[str] = "free"
    is_superuser: bool = False
    is_active: bool = True


# ---------------------------------------------------------------------------
# Fixture: install a resolver override for the WS endpoint
# ---------------------------------------------------------------------------


@pytest.fixture()
def patch_resolver(monkeypatch):
    """Return a setter that installs the next fake user for the WS handler.

    Usage::

        def test_x(client, patch_resolver):
            patch_resolver(_FakeWSUser(tier="pro"))
            with client.websocket_connect("/ws/sessions/abc/live") as ws:
                ...

    Passing ``None`` simulates the unauthed path — `_resolve_ws_user`
    returns None, the handler should close with 4001 before accept.
    """
    from api import streaming as streaming_mod

    state = {"user": None}

    def fake_resolver(websocket):  # noqa: ARG001
        return state["user"]

    monkeypatch.setattr(streaming_mod, "_resolve_ws_user", fake_resolver)

    def setter(user):
        state["user"] = user

    return setter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_free_tier_user_rejected_with_4003(client, patch_resolver):
    """Free-tier user: receives a JSON error frame, then close code 4003.

    The handler must accept the upgrade before sending so the JSON frame
    actually reaches the client; closing before accept would manifest as
    a generic 1006/handshake failure with no payload, which makes
    debugging painful.
    """
    patch_resolver(_FakeWSUser(email="free@m.local", tier="free"))

    url = "/ws/sessions/test-free/live"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(url) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error", msg
            assert msg["reason"] == "tier_insufficient", msg
            assert msg["current_tier"] == "free", msg
            assert msg["required_feature"] == "server_live", msg
            # Flat pricing (Free vs paid): the hint no longer names specific
            # tiers — a free user is simply told a paid plan is required.
            assert "paid plan" in msg["upgrade_hint"].lower(), msg["upgrade_hint"]
            # Next receive triggers the close.
            ws.receive_json()

    assert exc_info.value.code == 4003, exc_info.value.code
    # Starlette surfaces the close reason on WebSocketDisconnect on
    # recent versions; treat as best-effort.
    reason = getattr(exc_info.value, "reason", None)
    if reason is not None:
        assert reason == "tier_insufficient", reason


def _seed_free_org_session():
    """Create a session owned by a free-plan org; return its session_id.

    Organization.plan defaults to "free", so this is the active-workspace the
    per-workspace billing gate must reject for a server-live stream.
    """
    import uuid as _uuid

    from auth.models import Organization, User, UserOrganization
    from auth.utils import get_password_hash
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    try:
        org = Organization(
            name="Free Workspace", slug=f"free-ws-{_uuid.uuid4().hex[:8]}",
            is_active=True, plan="free",
        )
        db.add(org); db.commit(); db.refresh(org)
        user = User(
            email=f"owner-{_uuid.uuid4().hex[:6]}@m.local",
            username=f"owner_{_uuid.uuid4().hex[:6]}",
            hashed_password=get_password_hash("admin123"),
            is_active=True, is_verified=True, tier="free",
        )
        db.add(user); db.commit(); db.refresh(user)
        db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
        sid = str(_uuid.uuid4())
        db.add(RecordingSession(
            session_id=sid, name=f"ws-{sid[:8]}", title=f"ws-{sid[:8]}",
            meeting_type="meeting", mode="upload", status="recording",
            duration=0.0, user_id=user.id, organization_id=org.id,
            processing_metadata={},
        ))
        db.commit()
        return sid
    finally:
        db.close()


def test_paid_user_rejected_when_active_workspace_is_free(client, patch_resolver):
    """billing-1: a pro USER streaming into a FREE workspace is rejected.

    Even though the user's global tier has server_live, the active workspace
    (the org that owns the session) is on the free plan, so the per-workspace
    gate closes 4003 with a workspace-specific hint. This is the core of the
    UC dev's ruling: gate on the active workspace's entitlement, not the user.
    """
    sid = _seed_free_org_session()
    patch_resolver(_FakeWSUser(email="pro@m.local", tier="pro"))

    url = f"/ws/sessions/{sid}/live"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(url) as ws:
            msg = ws.receive_json()
            assert msg["type"] == "error", msg
            assert msg["reason"] == "tier_insufficient", msg
            assert "workspace" in msg["upgrade_hint"].lower(), msg["upgrade_hint"]
            ws.receive_json()  # triggers the close

    assert exc_info.value.code == 4003, exc_info.value.code


def test_pro_tier_user_receives_ready(client, patch_resolver):
    """Pro-tier user: receives `{type:"ready"}` and the connection stays open."""
    patch_resolver(_FakeWSUser(email="pro@m.local", tier="pro"))

    url = "/ws/sessions/test-pro/live"

    with client.websocket_connect(url) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ready", msg
        assert msg["session_id"] == "test-pro"
        assert msg["phase"] == "B.2"
        assert msg["tier"] == "pro"
        # Close cleanly so we don't leak the connection.
        ws.send_text('{"type":"end"}')
        # Server emits {type:"closed", ...} before 1000 close; read it
        # so the test isn't racing the close frame.
        closing = ws.receive_json()
        assert closing.get("type") in ("closed", "final"), closing


def test_enterprise_tier_user_receives_ready(client, patch_resolver):
    """Enterprise-tier user: same accept path as pro."""
    patch_resolver(_FakeWSUser(email="ent@m.local", tier="enterprise"))

    url = "/ws/sessions/test-ent/live"

    with client.websocket_connect(url) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ready", msg
        assert msg["tier"] == "enterprise"
        ws.send_text('{"type":"end"}')
        ws.receive_json()  # drain closing frame


def test_superuser_with_free_tier_bypasses_gate(client, patch_resolver):
    """is_superuser=True overrides tier='free' to enterprise via
    `get_user_tier()`; this is the support-account guarantee from
    `auth/tier.py`. A free-column superuser must NOT be tier-rejected."""
    patch_resolver(
        _FakeWSUser(email="admin@m.local", tier="free", is_superuser=True)
    )

    url = "/ws/sessions/test-super/live"

    with client.websocket_connect(url) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "ready", msg
        # `get_user_tier()` flips this to enterprise for superusers.
        assert msg["tier"] == "enterprise", msg
        ws.send_text('{"type":"end"}')
        ws.receive_json()


def test_unauthed_connect_still_closes_4001(client, patch_resolver):
    """No headers / unresolved user: B.1 unauth path closes 4001 before
    accept. Regression check that the B.4 gate didn't accidentally move
    the auth reject behind the accept (which would change the error
    surface from a clean handshake-reject to an in-band JSON + 4003)."""
    patch_resolver(None)  # resolver returns None ⇒ unauthed branch

    url = "/ws/sessions/test-unauth/live"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(url) as ws:
            # No frame should ever arrive — accept never runs.
            ws.receive_text()

    assert exc_info.value.code == 4001, exc_info.value.code
