"""Tests for multi-organization scoping."""

from __future__ import annotations

import uuid

from auth.utils import get_password_hash


def _current_models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession

    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _login_headers(client, username: str, password: str, org_slug: str | None = None) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _seed_user(db, username: str, password: str, email: str):
    _, User, _, _, _ = _current_models()
    user = User(
        email=email,
        username=username,
        hashed_password=get_password_hash(password),
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_org_isolation_blocks_cross_org_session_access(client):
    Organization, _, UserOrganization, SessionLocal, RecordingSession = _current_models()
    db = SessionLocal()
    try:
        org_x = Organization(name=f"Org X {uuid.uuid4().hex[:6]}", slug=f"org-x-{uuid.uuid4().hex[:6]}", is_active=True)
        org_y = Organization(name=f"Org Y {uuid.uuid4().hex[:6]}", slug=f"org-y-{uuid.uuid4().hex[:6]}", is_active=True)
        db.add_all([org_x, org_y])
        db.commit()
        db.refresh(org_x)
        db.refresh(org_y)

        user_a = _seed_user(db, f"user_a_{uuid.uuid4().hex[:6]}", "Password123", f"a_{uuid.uuid4().hex[:6]}@example.com")
        user_b = _seed_user(db, f"user_b_{uuid.uuid4().hex[:6]}", "Password123", f"b_{uuid.uuid4().hex[:6]}@example.com")
        db.add_all([
            UserOrganization(user_id=user_a.id, organization_id=org_x.id, role="user"),
            UserOrganization(user_id=user_b.id, organization_id=org_y.id, role="user"),
        ])
        db.commit()

        session_x = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="Org X Session",
            title="Org X Session",
            status="completed",
            organization_id=org_x.id,
            user_id=user_a.id,
        )
        session_y = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="Org Y Session",
            title="Org Y Session",
            status="completed",
            organization_id=org_y.id,
            user_id=user_b.id,
        )
        db.add_all([session_x, session_y])
        db.commit()
        user_a_username = user_a.username
        session_y_public_id = session_y.session_id
    finally:
        db.close()

    headers = _login_headers(client, user_a_username, "Password123")

    list_response = client.get("/api/simple/recording-sessions", headers=headers)
    assert list_response.status_code == 200
    sessions = list_response.json()["items"]
    assert [session["name"] for session in sessions] == ["Org X Session"]

    cross_org_response = client.get(
        f"/api/simple/recording-sessions/{session_y_public_id}",
        headers=headers,
    )
    assert cross_org_response.status_code == 404


def test_active_org_header_switches_visible_sessions(client):
    Organization, _, UserOrganization, SessionLocal, RecordingSession = _current_models()
    db = SessionLocal()
    try:
        org_a = Organization(name=f"Alpha {uuid.uuid4().hex[:6]}", slug=f"alpha-{uuid.uuid4().hex[:6]}", is_active=True)
        org_b = Organization(name=f"Beta {uuid.uuid4().hex[:6]}", slug=f"beta-{uuid.uuid4().hex[:6]}", is_active=True)
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        user = _seed_user(db, f"switcher_{uuid.uuid4().hex[:6]}", "Password123", f"switcher_{uuid.uuid4().hex[:6]}@example.com")
        db.add_all([
            UserOrganization(user_id=user.id, organization_id=org_a.id, role="user"),
            UserOrganization(user_id=user.id, organization_id=org_b.id, role="admin"),
        ])
        db.add_all([
            RecordingSession(
                session_id=str(uuid.uuid4()),
                name="Alpha Session",
                title="Alpha Session",
                status="completed",
                organization_id=org_a.id,
                user_id=user.id,
            ),
            RecordingSession(
                session_id=str(uuid.uuid4()),
                name="Beta Session",
                title="Beta Session",
                status="completed",
                organization_id=org_b.id,
                user_id=user.id,
            ),
        ])
        db.commit()
        username = user.username
        alpha_slug = org_a.slug
        beta_slug = org_b.slug
    finally:
        db.close()

    alpha_headers = _login_headers(client, username, "Password123", alpha_slug)
    alpha_response = client.get("/api/simple/recording-sessions", headers=alpha_headers)
    assert alpha_response.status_code == 200
    assert [session["name"] for session in alpha_response.json()["items"]] == ["Alpha Session"]

    beta_headers = _login_headers(client, username, "Password123", beta_slug)
    beta_response = client.get("/api/simple/recording-sessions", headers=beta_headers)
    assert beta_response.status_code == 200
    assert [session["name"] for session in beta_response.json()["items"]] == ["Beta Session"]
