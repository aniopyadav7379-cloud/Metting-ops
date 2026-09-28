from __future__ import annotations

import uuid

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import ActionItem, RecordingSession

    return Organization, User, UserOrganization, SessionLocal, RecordingSession, ActionItem


def _login_headers(client, username: str, password: str = "Password123") -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_user(db, suffix: str, label: str):
    _, User, _, _, _, _ = _models()
    user = User(
        email=f"{label}_{suffix}@example.com",
        username=f"{label}_{suffix}",
        hashed_password=get_password_hash("Password123"),
        is_active=True,
        is_verified=True,
        tier="enterprise",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_session(db, *, org_id: int, user_id: int, title: str):
    _, _, _, _, RecordingSession, _ = _models()
    session = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        status="completed",
        organization_id=org_id,
        user_id=user_id,
        transcript_simple=f"{title} transcript",
        transcript=f"{title} transcript",
        duration=90.0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@pytest.fixture()
def org_move_world(client):
    Organization, _, UserOrganization, SessionLocal, _, ActionItem = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:8]
    try:
        org_a = Organization(name=f"Move Alpha {suffix}", slug=f"move-alpha-{suffix}", is_active=True)
        org_b = Organization(name=f"Move Bravo {suffix}", slug=f"move-bravo-{suffix}", is_active=True)
        org_c = Organization(name=f"Move Charlie {suffix}", slug=f"move-charlie-{suffix}", is_active=True)
        db.add_all([org_a, org_b, org_c])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)
        db.refresh(org_c)

        owner = _seed_user(db, suffix, "owner")
        outsider = _seed_user(db, suffix, "outsider")
        db.add_all([
            UserOrganization(user_id=owner.id, organization_id=org_a.id, role="user"),
            UserOrganization(user_id=owner.id, organization_id=org_b.id, role="user"),
            UserOrganization(user_id=outsider.id, organization_id=org_b.id, role="user"),
        ])
        db.commit()

        session_a = _seed_session(db, org_id=org_a.id, user_id=owner.id, title=f"Alpha Session {suffix}")
        session_b = _seed_session(db, org_id=org_b.id, user_id=owner.id, title=f"Bravo Session {suffix}")
        session_c = _seed_session(db, org_id=org_c.id, user_id=outsider.id, title=f"Charlie Session {suffix}")
        item = ActionItem(
            session_id=session_a.id,
            organization_id=org_a.id,
            text="Follow up",
            owner="Aaron",
            status="todo",
            sort_order=0,
            source="manual",
        )
        db.add(item)
        db.commit()
        db.refresh(item)

        ctx = {
            "org_a_id": org_a.id,
            "org_a_slug": org_a.slug,
            "org_a_name": org_a.name,
            "org_b_id": org_b.id,
            "org_b_slug": org_b.slug,
            "org_b_name": org_b.name,
            "org_c_id": org_c.id,
            "org_c_slug": org_c.slug,
            "owner_username": owner.username,
            "outsider_username": outsider.username,
            "owner_id": owner.id,
            "outsider_id": outsider.id,
            "session_a_public_id": session_a.session_id,
            "session_a_db_id": session_a.id,
            "session_b_public_id": session_b.session_id,
            "session_b_db_id": session_b.id,
            "session_c_public_id": session_c.session_id,
            "action_item_id": item.id,
        }
    finally:
        db.close()

    ctx["owner_headers"] = _login_headers(client, ctx["owner_username"])
    ctx["outsider_headers"] = _login_headers(client, ctx["outsider_username"])
    return ctx


def test_owner_moves_session_to_org_they_belong_to(client, org_move_world):
    response = client.put(
        f"/api/simple/recording-sessions/{org_move_world['session_a_public_id']}/organization",
        json={"organization_id": org_move_world["org_b_id"]},
        headers=org_move_world["owner_headers"],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["organization_id"] == org_move_world["org_b_id"]
    assert body["organization_name"] == org_move_world["org_b_name"]

    _, _, _, SessionLocal, RecordingSession, _ = _models()
    db = SessionLocal()
    try:
        row = db.query(RecordingSession).filter(RecordingSession.id == org_move_world["session_a_db_id"]).one()
        assert row.organization_id == org_move_world["org_b_id"]
    finally:
        db.close()


def test_user_cannot_move_session_from_org_they_cannot_write(client, org_move_world):
    response = client.put(
        f"/api/simple/recording-sessions/{org_move_world['session_c_public_id']}/organization",
        json={"organization_id": org_move_world["org_b_id"]},
        headers=org_move_world["owner_headers"],
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "not_authorized_for_session"


def test_user_cannot_move_session_to_org_they_are_not_member_of(client, org_move_world):
    response = client.put(
        f"/api/simple/recording-sessions/{org_move_world['session_a_public_id']}/organization",
        json={"organization_id": org_move_world["org_c_id"]},
        headers=org_move_world["owner_headers"],
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "not_member_of_target_org"


def test_reassign_cascades_action_items_organization_id(client, org_move_world):
    response = client.put(
        f"/api/simple/recording-sessions/{org_move_world['session_a_public_id']}/organization",
        json={"organization_id": org_move_world["org_b_id"]},
        headers=org_move_world["owner_headers"],
    )
    assert response.status_code == 200, response.text

    _, _, _, SessionLocal, _, ActionItem = _models()
    db = SessionLocal()
    try:
        item = db.query(ActionItem).filter(ActionItem.id == org_move_world["action_item_id"]).one()
        assert item.organization_id == org_move_world["org_b_id"]
    finally:
        db.close()


def test_list_endpoint_respects_active_org_header(client, org_move_world):
    headers = {
        **org_move_world["owner_headers"],
        "X-MeetingOps-Org": org_move_world["org_a_slug"],
    }
    response = client.get("/api/simple/recording-sessions", headers=headers)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]

    assert any(row["id"] == org_move_world["session_a_public_id"] for row in rows)
    assert all(row["organization_id"] == org_move_world["org_a_id"] for row in rows)
    assert all(row["organization_name"] == org_move_world["org_a_name"] for row in rows)


def test_list_endpoint_can_include_all_user_orgs(client, org_move_world):
    headers = {
        **org_move_world["owner_headers"],
        "X-MeetingOps-Org": org_move_world["org_a_slug"],
    }
    response = client.get(
        "/api/simple/recording-sessions?include_all_my_orgs=true",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    ids = {row["id"] for row in rows}

    assert org_move_world["session_a_public_id"] in ids
    assert org_move_world["session_b_public_id"] in ids
    assert org_move_world["session_c_public_id"] not in ids
