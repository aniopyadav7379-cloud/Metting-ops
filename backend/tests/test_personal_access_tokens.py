from __future__ import annotations

import uuid

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, PersonalAccessToken, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession

    return Organization, PersonalAccessToken, User, UserOrganization, SessionLocal, RecordingSession


def _seed_user_org(*, slug: str, username: str):
    Organization, _, User, UserOrganization, SessionLocal, _ = _models()
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
                tier="pro",
            )
            db.add(user)
            db.commit()
            db.refresh(user)

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


def _seed_session(*, org_id: int, user_id: int, title: str = "PAT Test Session"):
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
            transcript_simple="Discussed project risks and next steps.",
            tags=[],
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session
    finally:
        db.close()


def _login_headers(client, username: str, org_slug: str | None = None):
    resp = client.post("/api/auth/login", data={"username": username, "password": "admin123"})
    assert resp.status_code == 200, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def test_create_returns_plaintext_once_and_list_hides_secret(client):
    _, PersonalAccessToken, _, _, SessionLocal, _ = _models()
    org, user = _seed_user_org(slug="pat-create", username="pat_create_user")
    headers = _login_headers(client, "pat_create_user", org.slug)

    resp = client.post("/api/auth/pats", headers=headers, json={"name": "Claude Desktop"})
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["plaintext"].startswith("mops_pat_")
    assert len(created["plaintext"]) == len("mops_pat_") + 32
    assert created["token_prefix"] == created["plaintext"][:12]

    list_resp = client.get("/api/auth/pats", headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    listed = list_resp.json()
    assert listed[0]["id"] == created["id"]
    assert "plaintext" not in listed[0]
    assert "token_hash" not in listed[0]
    assert created["plaintext"] not in list_resp.text

    db = SessionLocal()
    try:
        row = db.query(PersonalAccessToken).filter(PersonalAccessToken.user_id == user.id).first()
        assert row.token_hash
        assert row.token_hash != created["plaintext"]
    finally:
        db.close()


def test_resolve_pat_valid_user_and_updates_last_used(app):
    org, user = _seed_user_org(slug="pat-resolve", username="pat_resolve_user")
    from auth.pat import create_pat, resolve_pat
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        _, plaintext = create_pat(db, user=user, name="Resolve Test")
        db.expire_all()
        resolved = resolve_pat(db, plaintext=plaintext)
        assert resolved is not None
        assert resolved.id == user.id
        from auth.models import PersonalAccessToken

        row = db.query(PersonalAccessToken).filter(PersonalAccessToken.user_id == user.id).first()
        assert row.last_used_at is not None
    finally:
        db.close()


def test_resolve_pat_revoked_and_tampered_return_none(app):
    org, user = _seed_user_org(slug="pat-invalid", username="pat_invalid_user")
    from auth.pat import create_pat, resolve_pat, revoke_pat
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        row, plaintext = create_pat(db, user=user, name="Invalid Test")
        assert resolve_pat(db, plaintext=plaintext[:-1] + "A") is None
        assert revoke_pat(db, user=user, pat_id=row.id) is True
        assert resolve_pat(db, plaintext=plaintext) is None
    finally:
        db.close()


def test_auth_dependency_accepts_pat_and_jwt_still_works(client):
    org, user = _seed_user_org(slug="pat-auth", username="pat_auth_user")
    jwt_headers = _login_headers(client, "pat_auth_user", org.slug)

    jwt_resp = client.get("/api/auth/me", headers=jwt_headers)
    assert jwt_resp.status_code == 200, jwt_resp.text
    assert jwt_resp.json()["username"] == "pat_auth_user"

    from auth.pat import create_pat
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        _, plaintext = create_pat(db, user=user, name="Auth Test")
    finally:
        db.close()

    pat_resp = client.get(
        "/api/auth/me",
        headers={
            "Authorization": f"Bearer {plaintext}",
            "X-MeetingOps-Org": org.slug,
        },
    )
    assert pat_resp.status_code == 200, pat_resp.text
    assert pat_resp.json()["username"] == "pat_auth_user"


def test_cross_user_pat_cannot_read_other_users_org_data(client):
    org_a, user_a = _seed_user_org(slug="pat-org-a", username="pat_org_a_user")
    org_b, user_b = _seed_user_org(slug="pat-org-b", username="pat_org_b_user")
    session_b = _seed_session(org_id=org_b.id, user_id=user_b.id, title="User B Private Session")

    from auth.pat import create_pat
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        _, plaintext = create_pat(db, user=user_a, name="User A MCP")
    finally:
        db.close()

    denied = client.get(
        f"/api/simple/recording-sessions/{session_b.session_id}",
        headers={
            "Authorization": f"Bearer {plaintext}",
            "X-MeetingOps-Org": org_b.slug,
        },
    )
    assert denied.status_code == 403
