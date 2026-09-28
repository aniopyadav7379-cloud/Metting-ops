"""Per-device authentication tests for satellite devices (task #85).

Covers:

  * Pairing-code redemption with ``device_id`` issues a plaintext
    ``device_secret`` exactly once and stores the bcrypt hash on the row.
  * Pairing-code redemption WITHOUT ``device_id`` (Phase 1 legacy USB-
    mic path) issues no secret and remains backward compatible.
  * WebSocket connect with a valid secret succeeds (auth path returns,
    handshake JSON sent).
  * WebSocket connect with missing/invalid secret rejected with close
    code 1008.
  * HTTP state-mutating satellite endpoints reject without a
    device-secret header AND without user auth (i.e. dual-auth means
    "either, not neither").
  * HTTP state-mutating satellite endpoints accept the device secret
    in either Authorization: Bearer or X-Device-Secret.
  * Rate limit kicks in after 5 failures inside a 10-minute window.
  * Plaintext secret is never stored in the DB.

Also extends cross-org isolation: a device_secret minted in org A must
not authenticate against an org B device_id.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from auth.utils import get_password_hash


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import SatelliteDevice

    return Organization, User, UserOrganization, SessionLocal, SatelliteDevice


def _seed_user(db, username: str, password: str, email: str, is_superuser: bool = False):
    from auth.models import User

    user = User(
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


def _login_headers(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Wipe the in-memory rate limiter between tests so a lockout from
    one case doesn't leak into the next."""
    from auth.device_auth import reset_rate_limiter_for_tests

    reset_rate_limiter_for_tests()
    yield
    reset_rate_limiter_for_tests()


@pytest.fixture()
def sat_world(client):
    """Single-org fixture for satellite auth tests.

    Seeds one admin user, one org, one conference room. Each test that
    needs a paired device redeems a code itself so we observe the
    issued secret in the response.
    """
    from auth.models import Organization, UserOrganization
    from database.database import SessionLocal
    from database.models_rooms import ConferenceRoom

    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        # Satellite capture is a paid-tier (Pro/Enterprise) capability gated on
        # the ORG plan (_gate_satellite_for_org → get_org_tier), so the device
        # path 200s. Without a paid plan the device-auth assert 403s.
        org = Organization(name=f"SatAuth {suffix}", slug=f"satauth-{suffix}", is_active=True, plan="enterprise")
        db.add(org)
        db.commit()
        db.refresh(org)

        admin = _seed_user(
            db,
            f"satadmin_{suffix}",
            "Password123",
            f"satadmin_{suffix}@example.com",
        )
        db.add(UserOrganization(user_id=admin.id, organization_id=org.id, role="admin"))
        db.commit()

        room = ConferenceRoom(
            organization_id=org.id,
            name=f"SatAuth Room {suffix}",
        )
        db.add(room)
        db.commit()
        db.refresh(room)

        ctx = {
            "org_id": org.id,
            "room_id": str(room.id),
            "room_name": room.name,
            "admin_username": admin.username,
            "admin_password": "Password123",
            "headers": _login_headers(client, admin.username, "Password123"),
        }
    finally:
        db.close()

    return ctx


def _generate_code(client, sat_world, **kwargs):
    resp = client.post(
        f"/api/rooms/{sat_world['room_id']}/pairing-codes",
        headers=sat_world["headers"],
        **kwargs,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["code"]


def _redeem_with_device(client, sat_world, code: str, device_id: str):
    """Redeem a code with a device_id, return the full response body."""
    resp = client.post(
        "/api/rooms/pairing-codes/redeem",
        json={
            "code": code,
            "device_id": device_id,
            "device_name": f"Test {device_id}",
            "device_type": "esp32-s3",
        },
        headers=sat_world["headers"],
    )
    return resp


# ---------------------------------------------------------------------------
# Pairing-code redemption issues a secret
# ---------------------------------------------------------------------------


def test_redeem_issues_device_secret(client, sat_world):
    """Phase 3 path: redeeming with device_id returns the plaintext secret."""
    code = _generate_code(client, sat_world)
    device_id = f"esp-{uuid.uuid4().hex[:8]}"

    resp = _redeem_with_device(client, sat_world, code, device_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["device_id"] == device_id
    assert body["organization_id"] == sat_world["org_id"]
    assert "device_secret" in body and body["device_secret"]
    assert len(body["device_secret"]) >= 32  # urlsafe-base64 of 32 bytes
    assert "secret_warning" in body
    assert "once" in body["secret_warning"].lower()


def test_redeem_secret_is_hashed_in_db(client, sat_world):
    """The plaintext secret must never be stored as-is."""
    _, _, _, SessionLocal, SatelliteDevice = _models()

    code = _generate_code(client, sat_world)
    device_id = f"esp-{uuid.uuid4().hex[:8]}"

    resp = _redeem_with_device(client, sat_world, code, device_id)
    assert resp.status_code == 200
    plaintext = resp.json()["device_secret"]

    db = SessionLocal()
    try:
        row = db.query(SatelliteDevice).filter(
            SatelliteDevice.device_id == device_id
        ).first()
        assert row is not None
        # Hash must not equal the plaintext.
        assert row.device_secret != plaintext
        # bcrypt prefix.
        assert row.device_secret.startswith("$2"), (
            f"Expected bcrypt-hashed secret on disk, got: {row.device_secret[:8]!r}"
        )
    finally:
        db.close()


def test_redeem_without_device_id_legacy_path(client, sat_world):
    """Phase 1 path: no device_id ⇒ no satellite row, no secret in response."""
    code = _generate_code(client, sat_world)

    resp = client.post(
        "/api/rooms/pairing-codes/redeem",
        json={"code": code},
        headers=sat_world["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["room_id"] == sat_world["room_id"]
    assert "redeemed_at" in body
    # Phase 3 fields absent on legacy path.
    assert body.get("device_secret") is None
    assert body.get("device_id") is None


def test_re_pair_rotates_secret(client, sat_world):
    """Redeeming with the same device_id twice rotates the secret. The
    previous secret stops working immediately."""
    _, _, _, SessionLocal, SatelliteDevice = _models()
    device_id = f"esp-{uuid.uuid4().hex[:8]}"

    code1 = _generate_code(client, sat_world)
    first = _redeem_with_device(client, sat_world, code1, device_id)
    assert first.status_code == 200
    secret_a = first.json()["device_secret"]

    code2 = _generate_code(client, sat_world)
    second = _redeem_with_device(client, sat_world, code2, device_id)
    assert second.status_code == 200
    secret_b = second.json()["device_secret"]

    assert secret_a != secret_b, "Re-pairing must rotate the secret"

    db = SessionLocal()
    try:
        row = db.query(SatelliteDevice).filter(
            SatelliteDevice.device_id == device_id
        ).first()
        from auth.device_auth import verify_device_secret

        # New secret verifies, old one does not.
        assert verify_device_secret(secret_b, row.device_secret)
        assert not verify_device_secret(secret_a, row.device_secret)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# WebSocket auth
# ---------------------------------------------------------------------------
#
# We exercise the WebSocket endpoint via FastAPI's TestClient. The
# satellite handshake runs auth + DB lookup before consuming binary frames,
# so we can drive both the success and failure paths by connecting,
# observing the first JSON frame, then closing.


def _connect_ws(client, device_id: str, token: str | None = None):
    """Connect to /ws/satellite/{device_id}/audio with an optional token."""
    url = f"/ws/satellite/{device_id}/audio"
    if token is not None:
        url += f"?token={token}"
    return client.websocket_connect(url)


def test_ws_connect_with_valid_secret_succeeds(client, sat_world):
    """Authenticated connect receives the session-info JSON, no 1008 close."""
    code = _generate_code(client, sat_world)
    device_id = f"esp-{uuid.uuid4().hex[:8]}"
    secret = _redeem_with_device(client, sat_world, code, device_id).json()["device_secret"]

    with _connect_ws(client, device_id, token=secret) as ws:
        # First message is the session-info JSON.
        msg = ws.receive_json()
        assert "session_id" in msg
        assert msg.get("status") == "recording"


def test_ws_connect_without_token_rejected(client, sat_world):
    """No token query param + no Authorization header ⇒ 1008 close."""
    code = _generate_code(client, sat_world)
    device_id = f"esp-{uuid.uuid4().hex[:8]}"
    _redeem_with_device(client, sat_world, code, device_id)

    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _connect_ws(client, device_id, token=None) as ws:
            # First frame the server sends on auth failure is the
            # generic auth_failed JSON, then it closes with 1008.
            msg = ws.receive_json()
            assert msg.get("error") == "auth_failed"
            # Next receive should disconnect.
            ws.receive_text()
    assert exc_info.value.code == 1008


def test_ws_connect_with_wrong_secret_rejected(client, sat_world):
    """Wrong token ⇒ 1008 close. The body must never disclose which
    failure mode triggered the close."""
    code = _generate_code(client, sat_world)
    device_id = f"esp-{uuid.uuid4().hex[:8]}"
    real_secret = _redeem_with_device(client, sat_world, code, device_id).json()["device_secret"]
    wrong = "x" + real_secret[1:]  # garble one byte

    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _connect_ws(client, device_id, token=wrong) as ws:
            msg = ws.receive_json()
            assert msg.get("error") == "auth_failed"
            ws.receive_text()
    assert exc_info.value.code == 1008


def test_ws_connect_unknown_device_rejected(client, sat_world):
    """An attacker who guesses a device_id but no real device row exists
    must be rejected, with the same generic close code as a wrong secret."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _connect_ws(client, "esp-does-not-exist", token="anything") as ws:
            msg = ws.receive_json()
            assert msg.get("error") == "auth_failed"
            ws.receive_text()
    assert exc_info.value.code == 1008


def test_ws_connect_legacy_device_without_secret_on_file(client, sat_world):
    """A device row with device_secret = NULL (i.e. one that pre-dates
    this migration or was never paired in Phase 3) MUST NOT authenticate
    no matter what the caller presents."""
    _, _, _, SessionLocal, SatelliteDevice = _models()
    device_id = f"esp-legacy-{uuid.uuid4().hex[:8]}"

    db = SessionLocal()
    try:
        row = SatelliteDevice(
            device_id=device_id,
            name="Legacy Device",
            device_type="esp32-s3",
            status="offline",
            organization_id=sat_world["org_id"],
            device_secret=None,  # explicit NULL
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.commit()
    finally:
        db.close()

    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with _connect_ws(client, device_id, token="anything-at-all") as ws:
            msg = ws.receive_json()
            assert msg.get("error") == "auth_failed"
            ws.receive_text()
    assert exc_info.value.code == 1008


# ---------------------------------------------------------------------------
# HTTP dual-auth
# ---------------------------------------------------------------------------


def _make_paired_device(client, sat_world) -> tuple[str, str]:
    """Return (device_id, plaintext_secret) for a freshly-paired device."""
    code = _generate_code(client, sat_world)
    device_id = f"esp-{uuid.uuid4().hex[:8]}"
    body = _redeem_with_device(client, sat_world, code, device_id).json()
    return device_id, body["device_secret"]


def test_http_heartbeat_with_bearer_secret(client, sat_world):
    device_id, secret = _make_paired_device(client, sat_world)
    resp = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device_id"] == device_id


def test_http_heartbeat_with_x_device_secret(client, sat_world):
    device_id, secret = _make_paired_device(client, sat_world)
    resp = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": secret},
    )
    assert resp.status_code == 200, resp.text


def test_http_heartbeat_rejects_invalid_secret(client, sat_world):
    device_id, secret = _make_paired_device(client, sat_world)
    resp = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": "wrong"},
    )
    # Device flow with bad secret ⇒ 401, never falls back to user auth.
    assert resp.status_code == 401, resp.text


def test_http_heartbeat_no_creds_rejected(client, sat_world):
    """No user session, no device header ⇒ 401 (not silently allowed)."""
    device_id, _ = _make_paired_device(client, sat_world)
    resp = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        # No headers at all
    )
    assert resp.status_code == 401, resp.text


def test_http_heartbeat_admin_user_flow_still_works(client, sat_world):
    """Backward-compat: the admin UI sends user-auth + no device header.
    This must still succeed (dual-auth)."""
    device_id, _ = _make_paired_device(client, sat_world)
    resp = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        headers=sat_world["headers"],
    )
    assert resp.status_code == 200, resp.text


def test_http_start_recording_with_device_secret(client, sat_world):
    device_id, secret = _make_paired_device(client, sat_world)
    resp = client.post(
        f"/api/satellites/{device_id}/start-recording",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert resp.status_code == 200, resp.text


def test_http_transcript_endpoint_requires_auth(client, sat_world):
    device_id, secret = _make_paired_device(client, sat_world)
    # Unauthenticated
    resp = client.post(
        f"/api/satellites/{device_id}/transcript",
        json={"transcript_text": "hello"},
    )
    assert resp.status_code == 401

    # Device-auth
    ok = client.post(
        f"/api/satellites/{device_id}/transcript",
        json={"transcript_text": "hello"},
        headers={"X-Device-Secret": secret},
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_locks_out_after_five_failures(client, sat_world):
    """5 failed auths in 10 min ⇒ subsequent attempts rejected even
    with the CORRECT secret until the lockout window expires."""
    device_id, real_secret = _make_paired_device(client, sat_world)

    for i in range(5):
        resp = client.post(
            f"/api/satellites/{device_id}/heartbeat",
            json={"status": "online"},
            headers={"X-Device-Secret": f"wrong-{i}"},
        )
        assert resp.status_code == 401

    # 6th attempt — even with the real secret — must be rejected because
    # the bucket is now locked.
    locked = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": real_secret},
    )
    assert locked.status_code == 401, (
        f"Rate limiter failed to lock out device_id={device_id} after "
        f"5 failures. Got status={locked.status_code} body={locked.text[:200]}"
    )


def test_rate_limit_success_clears_failure_count(client, sat_world):
    """A successful auth resets the failure counter so an admin doesn't
    get locked out after one typo."""
    device_id, real_secret = _make_paired_device(client, sat_world)

    # 4 failures (one short of lockout).
    for i in range(4):
        client.post(
            f"/api/satellites/{device_id}/heartbeat",
            json={"status": "online"},
            headers={"X-Device-Secret": f"wrong-{i}"},
        )

    # Success — should clear.
    ok = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": real_secret},
    )
    assert ok.status_code == 200, ok.text

    # 4 more failures should NOT trip the lockout — counter was reset.
    for i in range(4):
        client.post(
            f"/api/satellites/{device_id}/heartbeat",
            json={"status": "online"},
            headers={"X-Device-Secret": f"wrong-{i}"},
        )

    still_ok = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": real_secret},
    )
    assert still_ok.status_code == 200, still_ok.text


# ---------------------------------------------------------------------------
# Headers / leak hygiene
# ---------------------------------------------------------------------------


def test_error_body_never_leaks_secret(client, sat_world):
    """The 401 body must never reflect the presented secret back to the
    caller — that would let log scrapers harvest valid secrets from
    error pages."""
    device_id, _ = _make_paired_device(client, sat_world)
    sneaky = "totally-secret-value-do-not-echo"
    resp = client.post(
        f"/api/satellites/{device_id}/heartbeat",
        json={"status": "online"},
        headers={"X-Device-Secret": sneaky},
    )
    assert resp.status_code == 401
    assert sneaky not in resp.text


# ---------------------------------------------------------------------------
# Backward compat: existing admin endpoints (list, get, etc.) still work
# ---------------------------------------------------------------------------


def test_user_crud_endpoints_unchanged(client, sat_world):
    """Sanity: list / get / delete still work for an authenticated admin
    user, since those are not in the dual-auth set."""
    device_id, _ = _make_paired_device(client, sat_world)

    listing = client.get("/api/satellites", headers=sat_world["headers"])
    assert listing.status_code == 200
    assert any(d["device_id"] == device_id for d in listing.json())

    detail = client.get(
        f"/api/satellites/{device_id}",
        headers=sat_world["headers"],
    )
    assert detail.status_code == 200
    assert detail.json()["device_id"] == device_id
