from __future__ import annotations

import uuid

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession

    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _seed_user_org(*, slug: str, username: str):
    Organization, User, UserOrganization, SessionLocal, _ = _models()
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

        if not db.query(UserOrganization).filter(
            UserOrganization.user_id == user.id,
            UserOrganization.organization_id == org.id,
        ).first():
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
            db.commit()
        db.refresh(org)
        db.refresh(user)
        return org, user
    finally:
        db.close()


def _seed_session(*, org_id: int, user_id: int, title: str):
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        session = RecordingSession(
            session_id=str(uuid.uuid4()),
            title=title,
            name=title,
            status="completed",
            user_id=user_id,
            organization_id=org_id,
            tags=[],
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session
    finally:
        db.close()


def test_mcp_pat_propose_rename_cross_user_is_rejected_by_backend(client):
    org_a, user_a = _seed_user_org(slug="mcp-pat-a", username="mcp_pat_a_user")
    org_b, user_b = _seed_user_org(slug="mcp-pat-b", username="mcp_pat_b_user")
    session_b = _seed_session(org_id=org_b.id, user_id=user_b.id, title="Owned By User B")

    from auth.pat import create_pat
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        _, plaintext = create_pat(db, user=user_a, name="Claude Desktop")
    finally:
        db.close()

    response = client.post(
        "/api/agent-actions/propose",
        headers={
            "Authorization": f"Bearer {plaintext}",
            "X-MeetingOps-Org": org_b.slug,
        },
        json={
            "action": "rename_session",
            "payload": {
                "session_id": session_b.session_id,
                "title": "Should Not Apply",
            },
        },
    )

    assert response.status_code == 403
    assert "access" in response.text.lower()
