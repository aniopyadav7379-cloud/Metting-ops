"""Tests for the Conference Room API (CR-003) + RoomRecorder service (CR-004).

Covers:
  * Room CRUD: create / list / get / update / delete, with cross-org and
    non-admin refusal paths.
  * Audio source attach/list/remove, including hardware_type validation
    and the server_usb_mic device_path enforcement.
  * Pairing code generation (uniqueness, expiry, redemption, org scoping
    on redeem).
  * ACL grant/revoke + per-room access enforcement (admin / member / viewer).
  * Recording lifecycle (start / stop) with the RoomRecorder service
    patched at the import boundary so we don't spawn arecord in tests.

The recorder integration test patches ``services.room_recorder.start_room_recorder``
and ``stop_room_recorder`` to a pair of in-memory async stubs. The wiring
contract under test is "the API calls them with the right arguments and
records the session correctly" — actually exec'ing arecord requires real
ALSA hardware and is out of scope for unit tests.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from auth.utils import get_password_hash


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal

    return Organization, User, UserOrganization, SessionLocal


def _login_headers(client, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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


@pytest.fixture()
def room_world(client):
    """Two orgs, one admin + one member + one outsider in org A, one admin in org B.

    Returns a context dict with login headers + ids for everyone. The
    tests then exercise read/write paths and assert org isolation +
    role enforcement.
    """
    Organization, User, UserOrganization, SessionLocal = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        org_a = Organization(name=f"Alpha {suffix}", slug=f"alpha-{suffix}", is_active=True, plan="pro")
        org_b = Organization(name=f"Bravo {suffix}", slug=f"bravo-{suffix}", is_active=True, plan="pro")
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        admin_a = _seed_user(db, f"admin_a_{suffix}", "Password123", f"adm_a_{suffix}@x.com")
        member_a = _seed_user(db, f"member_a_{suffix}", "Password123", f"mem_a_{suffix}@x.com")
        viewer_a = _seed_user(db, f"viewer_a_{suffix}", "Password123", f"vie_a_{suffix}@x.com")
        outsider_a = _seed_user(db, f"outsider_a_{suffix}", "Password123", f"out_a_{suffix}@x.com")
        admin_b = _seed_user(db, f"admin_b_{suffix}", "Password123", f"adm_b_{suffix}@x.com")

        db.add_all([
            UserOrganization(user_id=admin_a.id, organization_id=org_a.id, role="admin"),
            UserOrganization(user_id=member_a.id, organization_id=org_a.id, role="user"),
            UserOrganization(user_id=viewer_a.id, organization_id=org_a.id, role="user"),
            UserOrganization(user_id=outsider_a.id, organization_id=org_a.id, role="user"),
            UserOrganization(user_id=admin_b.id, organization_id=org_b.id, role="admin"),
        ])
        db.commit()

        # Room recording is the canonical_reprocess (Pro) gate — both the user
        # tier AND the active-org plan must cover it (billing-1). The users that
        # start/grant recordings are on Pro in this paid workspace.
        admin_a.tier = "pro"
        member_a.tier = "pro"
        db.commit()

        ctx = {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "admin_a_id": admin_a.id,
            "member_a_id": member_a.id,
            "viewer_a_id": viewer_a.id,
            "outsider_a_id": outsider_a.id,
            "admin_b_id": admin_b.id,
            "admin_a_username": admin_a.username,
            "member_a_username": member_a.username,
            "viewer_a_username": viewer_a.username,
            "outsider_a_username": outsider_a.username,
            "admin_b_username": admin_b.username,
        }
    finally:
        db.close()

    ctx["headers_admin_a"] = _login_headers(client, ctx["admin_a_username"], "Password123")
    ctx["headers_member_a"] = _login_headers(client, ctx["member_a_username"], "Password123")
    ctx["headers_viewer_a"] = _login_headers(client, ctx["viewer_a_username"], "Password123")
    ctx["headers_outsider_a"] = _login_headers(client, ctx["outsider_a_username"], "Password123")
    ctx["headers_admin_b"] = _login_headers(client, ctx["admin_b_username"], "Password123")
    return ctx


def _create_room(client, headers, name: str = "Boardroom") -> dict:
    resp = client.post(
        "/api/rooms",
        json={"name": name, "location": "Building A"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------


def test_create_room_requires_admin(client, room_world):
    """Non-admins cannot create rooms."""
    resp = client.post(
        "/api/rooms",
        json={"name": "Should not exist"},
        headers=room_world["headers_member_a"],
    )
    assert resp.status_code == 403


def test_create_and_list_room(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Alpha Room")
    assert room["name"] == "Alpha Room"
    assert room["status"] == "idle"
    assert room["organization_id"] == room_world["org_a_id"]

    resp = client.get("/api/rooms", headers=room_world["headers_admin_a"])
    assert resp.status_code == 200
    rooms = resp.json()
    assert any(r["id"] == room["id"] for r in rooms)


def test_room_duplicate_name_rejected(client, room_world):
    _create_room(client, room_world["headers_admin_a"], "UniqueName")
    resp = client.post(
        "/api/rooms",
        json={"name": "UniqueName"},
        headers=room_world["headers_admin_a"],
    )
    assert resp.status_code == 409


def test_room_cross_org_isolation(client, room_world):
    """Org B cannot list, fetch, update, or delete org A's rooms."""
    room = _create_room(client, room_world["headers_admin_a"], "ConfRoomA")
    list_b = client.get("/api/rooms", headers=room_world["headers_admin_b"])
    assert list_b.status_code == 200
    assert all(r["id"] != room["id"] for r in list_b.json())

    get_b = client.get(f"/api/rooms/{room['id']}", headers=room_world["headers_admin_b"])
    assert get_b.status_code == 404

    put_b = client.put(
        f"/api/rooms/{room['id']}",
        json={"name": "pwned"},
        headers=room_world["headers_admin_b"],
    )
    assert put_b.status_code == 404

    del_b = client.delete(
        f"/api/rooms/{room['id']}",
        headers=room_world["headers_admin_b"],
    )
    assert del_b.status_code == 404


def test_room_update_changes_metadata(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "UpdateMe")
    resp = client.put(
        f"/api/rooms/{room['id']}",
        json={"location": "New Wing", "default_retention_days": 365, "legal_hold": True},
        headers=room_world["headers_admin_a"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["location"] == "New Wing"
    assert body["default_retention_days"] == 365
    assert body["legal_hold"] is True


def test_room_delete(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "DeleteMe")
    resp = client.delete(
        f"/api/rooms/{room['id']}",
        headers=room_world["headers_admin_a"],
    )
    assert resp.status_code == 200
    follow = client.get(
        f"/api/rooms/{room['id']}",
        headers=room_world["headers_admin_a"],
    )
    assert follow.status_code == 404


# ---------------------------------------------------------------------------
# Audio sources
# ---------------------------------------------------------------------------


def test_add_source_validates_hardware_type(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Sources1")
    bad = client.post(
        f"/api/rooms/{room['id']}/sources",
        json={"hardware_type": "wat", "device_path": "hw:0,0"},
        headers=room_world["headers_admin_a"],
    )
    assert bad.status_code == 400


def test_add_source_usb_requires_device_path(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Sources2")
    bad = client.post(
        f"/api/rooms/{room['id']}/sources",
        json={"hardware_type": "server_usb_mic"},
        headers=room_world["headers_admin_a"],
    )
    assert bad.status_code == 400


def test_add_source_usb_bad_path(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Sources3")
    bad = client.post(
        f"/api/rooms/{room['id']}/sources",
        json={"hardware_type": "server_usb_mic", "device_path": "/dev/sda"},
        headers=room_world["headers_admin_a"],
    )
    assert bad.status_code == 400


def test_add_and_list_source(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Sources4")
    resp = client.post(
        f"/api/rooms/{room['id']}/sources",
        json={
            "hardware_type": "server_usb_mic",
            "device_path": "hw:2,0",
            "label": "Table mic",
        },
        headers=room_world["headers_admin_a"],
    )
    assert resp.status_code == 200, resp.text
    source = resp.json()
    assert source["device_path"] == "hw:2,0"
    assert source["label"] == "Table mic"

    list_resp = client.get(
        f"/api/rooms/{room['id']}/sources",
        headers=room_world["headers_admin_a"],
    )
    assert list_resp.status_code == 200
    assert any(s["id"] == source["id"] for s in list_resp.json())


def test_remove_source(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Sources5")
    src = client.post(
        f"/api/rooms/{room['id']}/sources",
        json={
            "hardware_type": "server_usb_mic",
            "device_path": "hw:1,0",
        },
        headers=room_world["headers_admin_a"],
    ).json()
    del_resp = client.delete(
        f"/api/rooms/{room['id']}/sources/{src['id']}",
        headers=room_world["headers_admin_a"],
    )
    assert del_resp.status_code == 200


# ---------------------------------------------------------------------------
# Pairing codes
# ---------------------------------------------------------------------------


def test_pairing_code_generation_and_uniqueness(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Pairing1")
    codes = set()
    for _ in range(5):
        resp = client.post(
            f"/api/rooms/{room['id']}/pairing-codes",
            headers=room_world["headers_admin_a"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"].isdigit() and len(body["code"]) == 6
        codes.add(body["code"])
    # Five generations should be unique while still active.
    assert len(codes) == 5


def test_pairing_code_requires_admin(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Pairing2")
    resp = client.post(
        f"/api/rooms/{room['id']}/pairing-codes",
        headers=room_world["headers_member_a"],
    )
    assert resp.status_code == 403


def test_pairing_code_redeem_marks_consumed(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "Pairing3")
    code_resp = client.post(
        f"/api/rooms/{room['id']}/pairing-codes",
        headers=room_world["headers_admin_a"],
    )
    code = code_resp.json()["code"]

    redeem = client.post(
        "/api/rooms/pairing-codes/redeem",
        json={"code": code},
        headers=room_world["headers_admin_a"],
    )
    assert redeem.status_code == 200, redeem.text
    assert redeem.json()["room_id"] == room["id"]

    # Second redemption is a 404 (consumed).
    second = client.post(
        "/api/rooms/pairing-codes/redeem",
        json={"code": code},
        headers=room_world["headers_admin_a"],
    )
    assert second.status_code == 404


def test_pairing_code_cross_org_redeem_denied(client, room_world):
    """A code minted in org A cannot be redeemed from an org B session."""
    room = _create_room(client, room_world["headers_admin_a"], "PairingX")
    code = (
        client.post(
            f"/api/rooms/{room['id']}/pairing-codes",
            headers=room_world["headers_admin_a"],
        )
        .json()["code"]
    )
    resp = client.post(
        "/api/rooms/pairing-codes/redeem",
        json={"code": code},
        headers=room_world["headers_admin_b"],
    )
    assert resp.status_code == 404


def test_pairing_code_bad_format(client, room_world):
    bad = client.post(
        "/api/rooms/pairing-codes/redeem",
        json={"code": "abcdef"},
        headers=room_world["headers_admin_a"],
    )
    # Pydantic accepts the string; the endpoint rejects non-digits.
    assert bad.status_code == 400


def test_pairing_code_expiry(client, room_world):
    """A code past expires_at must not redeem."""
    room = _create_room(client, room_world["headers_admin_a"], "PairingExpiry")
    code_resp = client.post(
        f"/api/rooms/{room['id']}/pairing-codes",
        headers=room_world["headers_admin_a"],
    )
    code = code_resp.json()["code"]

    # Hand-tune expiry into the past.
    from datetime import datetime, timedelta, timezone

    from database.database import SessionLocal
    from database.models_rooms import RoomPairingCode

    db = SessionLocal()
    try:
        row = db.query(RoomPairingCode).filter(RoomPairingCode.code == code).first()
        assert row is not None
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db.commit()
    finally:
        db.close()

    resp = client.post(
        "/api/rooms/pairing-codes/redeem",
        json={"code": code},
        headers=room_world["headers_admin_a"],
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# ACL
# ---------------------------------------------------------------------------


def test_acl_grant_and_revoke(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "AclRoom")
    grant = client.post(
        f"/api/rooms/{room['id']}/acl",
        json={"user_id": room_world["member_a_id"], "role": "member"},
        headers=room_world["headers_admin_a"],
    )
    assert grant.status_code == 200, grant.text
    assert grant.json()["role"] == "member"

    revoke = client.delete(
        f"/api/rooms/{room['id']}/acl/{room_world['member_a_id']}",
        headers=room_world["headers_admin_a"],
    )
    assert revoke.status_code == 200


def test_acl_grant_rejects_unknown_role(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "AclRole")
    resp = client.post(
        f"/api/rooms/{room['id']}/acl",
        json={"user_id": room_world["member_a_id"], "role": "godmode"},
        headers=room_world["headers_admin_a"],
    )
    assert resp.status_code == 400


def test_acl_grant_rejects_cross_org_user(client, room_world):
    room = _create_room(client, room_world["headers_admin_a"], "AclCross")
    resp = client.post(
        f"/api/rooms/{room['id']}/acl",
        json={"user_id": room_world["admin_b_id"], "role": "member"},
        headers=room_world["headers_admin_a"],
    )
    assert resp.status_code == 404


def test_acl_member_can_read_with_grant(client, room_world):
    """Member without ACL gets 403; with viewer grant gets 200."""
    room = _create_room(client, room_world["headers_admin_a"], "AclVisibility")
    # No grant yet — member cannot read details.
    no_grant = client.get(
        f"/api/rooms/{room['id']}",
        headers=room_world["headers_outsider_a"],
    )
    assert no_grant.status_code == 403

    # Grant viewer.
    client.post(
        f"/api/rooms/{room['id']}/acl",
        json={"user_id": room_world["outsider_a_id"], "role": "viewer"},
        headers=room_world["headers_admin_a"],
    )

    with_grant = client.get(
        f"/api/rooms/{room['id']}",
        headers=room_world["headers_outsider_a"],
    )
    assert with_grant.status_code == 200


# ---------------------------------------------------------------------------
# Recording lifecycle
# ---------------------------------------------------------------------------


@pytest.fixture()
def patched_recorder():
    """Replace start/stop with async stubs so the test doesn't shell out."""
    starts: list[dict] = []
    stops: list[uuid.UUID] = []

    async def fake_start(*, room, source, session):
        starts.append({
            "room_id": str(room.id),
            "source_id": str(source.id),
            "session_pk": session.id,
            "session_public_id": session.session_id,
        })
        return None

    async def fake_stop(*, room_id):
        stops.append(room_id)
        return True

    with (
        patch("services.room_recorder.start_room_recorder", new=AsyncMock(side_effect=fake_start)),
        patch("services.room_recorder.stop_room_recorder", new=AsyncMock(side_effect=fake_stop)),
    ):
        yield {"starts": starts, "stops": stops}


def test_recording_requires_source(client, room_world, patched_recorder):
    room = _create_room(client, room_world["headers_admin_a"], "RecNoSource")
    # No source — must 400.
    resp = client.post(
        f"/api/rooms/{room['id']}/recordings/start",
        headers=room_world["headers_admin_a"],
    )
    assert resp.status_code == 400


def test_recording_start_and_stop(client, room_world, patched_recorder):
    room = _create_room(client, room_world["headers_admin_a"], "RecLifecycle")
    # Add a source.
    client.post(
        f"/api/rooms/{room['id']}/sources",
        json={
            "hardware_type": "server_usb_mic",
            "device_path": "hw:2,0",
            "label": "Table mic",
        },
        headers=room_world["headers_admin_a"],
    )
    start = client.post(
        f"/api/rooms/{room['id']}/recordings/start",
        headers=room_world["headers_admin_a"],
    )
    assert start.status_code == 200, start.text
    body = start.json()
    assert body["room_id"] == room["id"]
    assert body["session_id"]
    assert len(patched_recorder["starts"]) == 1

    # Second start while running — 409.
    second = client.post(
        f"/api/rooms/{room['id']}/recordings/start",
        headers=room_world["headers_admin_a"],
    )
    assert second.status_code == 409

    # Stop.
    stop = client.post(
        f"/api/rooms/{room['id']}/recordings/stop",
        headers=room_world["headers_admin_a"],
    )
    assert stop.status_code == 200, stop.text
    assert len(patched_recorder["stops"]) == 1


def test_recording_member_can_start_with_grant(client, room_world, patched_recorder):
    """A non-admin member needs a per-room ACL of 'member' or 'admin' to start."""
    room = _create_room(client, room_world["headers_admin_a"], "MemberStart")
    client.post(
        f"/api/rooms/{room['id']}/sources",
        json={"hardware_type": "server_usb_mic", "device_path": "hw:0,0"},
        headers=room_world["headers_admin_a"],
    )

    # Without grant — outsider gets 403.
    no_grant = client.post(
        f"/api/rooms/{room['id']}/recordings/start",
        headers=room_world["headers_outsider_a"],
    )
    assert no_grant.status_code == 403

    # Grant member.
    client.post(
        f"/api/rooms/{room['id']}/acl",
        json={"user_id": room_world["outsider_a_id"], "role": "member"},
        headers=room_world["headers_admin_a"],
    )

    with_grant = client.post(
        f"/api/rooms/{room['id']}/recordings/start",
        headers=room_world["headers_outsider_a"],
    )
    assert with_grant.status_code == 200


# ---------------------------------------------------------------------------
# arecord -l parser
# ---------------------------------------------------------------------------


def test_arecord_parser_handles_usb():
    from api.rooms import _parse_arecord_l

    sample = (
        "**** List of CAPTURE Hardware Devices ****\n"
        "card 0: PCH [HDA Intel PCH], device 0: ALC892 Analog [ALC892 Analog]\n"
        "  Subdevices: 1/1\n"
        "  Subdevice #0: subdevice #0\n"
        "card 2: USB [Some USB Card], device 0: USB Audio [USB Audio]\n"
        "  Subdevices: 1/1\n"
    )
    rows = _parse_arecord_l(sample)
    assert len(rows) == 2
    paths = {r.device_path for r in rows}
    assert "hw:0,0" in paths
    assert "hw:2,0" in paths
    usb = next(r for r in rows if r.card == 2)
    assert usb.is_usb is True


def test_arecord_parser_empty_output():
    from api.rooms import _parse_arecord_l

    assert _parse_arecord_l("") == []
