"""Tests for Phase 2: Provider Registry, RAG, Embeddings, Digests, Integrations."""

from __future__ import annotations

import uuid
import json

from auth.utils import get_password_hash


def _current_models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession, OrgProviderSettings, MeetingDigest
    from database.database import Base, engine
    return Organization, User, UserOrganization, SessionLocal, RecordingSession, OrgProviderSettings, MeetingDigest


def _login_headers(client, username: str, password: str, org_slug: str | None = None) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, f"Login failed: {response.text}"
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _seed_user(db, username: str, password: str, email: str, is_superuser: bool = False):
    _, User, _, _, _, _, _ = _current_models()
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


def _seed_org(db, name: str, slug: str):
    Organization, _, _, _, _, _, _ = _current_models()
    org = Organization(name=name, slug=slug, is_active=True)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _seed_membership(db, user_id: int, org_id: int, role: str = "user"):
    _, _, UserOrganization, _, _, _, _ = _current_models()
    mem = UserOrganization(user_id=user_id, organization_id=org_id, role=role)
    db.add(mem)
    db.commit()
    return mem


def _seed_session(db, org_id: int, name: str, status: str = "completed"):
    _, _, _, _, RecordingSession, _, _ = _current_models()
    session = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=name,
        status=status,
        organization_id=org_id,
        duration=60.0,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


class TestProviderRegistry:
    """Provider settings CRUD with org scoping."""

    def test_list_providers_returns_defaults(self, client):
        _, _, _, SessionLocal, _, _, _ = _current_models()
        db = SessionLocal()
        try:
            _, _, _, _, _, OrgProviderSettings, _ = _current_models()
            db.query(OrgProviderSettings).delete()
            db.commit()

            user = _seed_user(db, "prov_test", "test123", "prov@test.com", is_superuser=True)
            org = _seed_org(db, "Provider Org", "provider-org")
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, "prov_test", "test123", "provider-org")

            resp = client.get("/api/providers", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 6
            kinds = [d["service_kind"] for d in data]
            assert "llm" in kinds
            assert "embeddings" in kinds
        finally:
            db.rollback()
            db.close()

    def test_admin_can_update_provider_settings(self, client):
        _, _, _, SessionLocal, _, _, _ = _current_models()
        db = SessionLocal()
        try:
            _, _, _, _, _, OrgProviderSettings, _ = _current_models()
            db.query(OrgProviderSettings).delete()
            db.commit()

            user = _seed_user(db, "prov_admin", "test123", "prov_admin@test.com")
            org = _seed_org(db, "Admin Org", "admin-org")
            _seed_membership(db, user.id, org.id, "admin")
            headers = _login_headers(client, "prov_admin", "test123", "admin-org")

            resp = client.put(
                "/api/providers/llm",
                json={
                    "provider_name": "litellm",
                    "model_name": "gemma-4-26b-moe",
                },
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["provider_name"] == "litellm"
            assert data["model_name"] == "gemma-4-26b-moe"

            # Verify via GET
            resp2 = client.get("/api/providers/llm", headers=headers)
            assert resp2.status_code == 200
            assert resp2.json()["model_name"] == "gemma-4-26b-moe"
        finally:
            db.rollback()
            db.close()

    def test_non_admin_cannot_edit_providers(self, client):
        _, _, _, SessionLocal, _, _, _ = _current_models()
        db = SessionLocal()
        try:
            _, _, _, _, _, OrgProviderSettings, _ = _current_models()
            db.query(OrgProviderSettings).delete()
            db.commit()

            user = _seed_user(db, "prov_user", "test123", "prov_user@test.com")
            org = _seed_org(db, "User Org", "user-org")
            _seed_membership(db, user.id, org.id, "user")
            headers = _login_headers(client, "prov_user", "test123", "user-org")

            resp = client.put(
                "/api/providers/llm",
                json={"provider_name": "openai"},
                headers=headers,
            )
            assert resp.status_code == 403
        finally:
            db.rollback()
            db.close()


class TestProviderSettingsOrgIsolation:
    """Provider settings are scoped per-org."""

    def test_org_a_cannot_see_org_b_settings(self, client):
        _, _, _, SessionLocal, _, OrgProviderSettings, _ = _current_models()
        db = SessionLocal()
        try:
            db.query(OrgProviderSettings).delete()
            db.commit()

            user = _seed_user(db, "iso_prov", "test123", "iso_prov@test.com", is_superuser=True)
            org_a = _seed_org(db, "Org A", "org-a")
            org_b = _seed_org(db, "Org B", "org-b")
            _seed_membership(db, user.id, org_a.id, "admin")
            _seed_membership(db, user.id, org_b.id, "admin")

            # Set in org_a
            h_a = _login_headers(client, "iso_prov", "test123", "org-a")
            client.put("/api/providers/llm", json={"provider_name": "anthropic"}, headers=h_a)

            # Set in org_b
            h_b = _login_headers(client, "iso_prov", "test123", "org-b")
            client.put("/api/providers/llm", json={"provider_name": "openai"}, headers=h_b)

            # Verify org_a sees its own
            resp_a = client.get("/api/providers/llm", headers=h_a)
            assert resp_a.json()["provider_name"] == "anthropic"

            # Verify org_b sees its own
            resp_b = client.get("/api/providers/llm", headers=h_b)
            assert resp_b.json()["provider_name"] == "openai"
        finally:
            db.rollback()
            db.close()


class TestDigestsEndpoint:
    """Time-window digests endpoint."""

    def test_digests_requires_auth(self, client):
        resp = client.get("/api/digests?period=day&date=2026-05-03")
        assert resp.status_code in (401, 403)

    def test_digests_accepts_valid_params(self, client):
        _, _, _, SessionLocal, _, _, _ = _current_models()
        db = SessionLocal()
        try:
            user = _seed_user(db, "dig_user", "test123", "dig@test.com", is_superuser=True)
            org = _seed_org(db, "Digest Org", "digest-org")
            _seed_membership(db, user.id, org.id, "user")
            headers = _login_headers(client, "dig_user", "test123", "digest-org")

            resp = client.get("/api/digests?period=day&date=2026-05-03", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            assert data["period"] == "day"
        finally:
            db.close()


class TestIntegrations:
    """Project integrations endpoint."""

    def test_integrations_returns_list(self, client):
        """Endpoint proxies to project-ops with the user's bearer token.
        In the test environment project-ops is not reachable, so the
        response is an empty list (graceful degrade rather than 5xx)."""
        _, _, _, SessionLocal, _, _, _ = _current_models()
        db = SessionLocal()
        try:
            user = _seed_user(db, "int_user", "test123", "int@test.com", is_superuser=True)
            org = _seed_org(db, "Integration Org", "int-org")
            _seed_membership(db, user.id, org.id, "user")
            headers = _login_headers(client, "int_user", "test123", "int-org")

            resp = client.get(f"/api/integrations/project-ops/projects?organization_id={org.id}", headers=headers)
            assert resp.status_code == 200
            data = resp.json()
            # Either a real list of projects (when project-ops is reachable
            # and the bearer token is accepted) or an empty list (test env).
            # Both are valid; the picker tolerates both shapes. Each entry
            # uses string IDs (project-ops UUIDs).
            assert isinstance(data, list)
            for entry in data:
                assert entry["app"] == "project-ops"
                assert isinstance(entry["id"], str)
        finally:
            db.close()

    def test_integrations_respects_org(self, client):
        _, _, _, SessionLocal, _, _, _ = _current_models()
        db = SessionLocal()
        try:
            user = _seed_user(db, "int_iso", "test123", "int_iso@test.com", is_superuser=False)
            org_a = _seed_org(db, "Int A", "int-a")
            org_b = _seed_org(db, "Int B", "int-b")
            _seed_membership(db, user.id, org_a.id, "user")
            _seed_membership(db, user.id, org_b.id, "user")
            headers = _login_headers(client, "int_iso", "test123", "int-a")

            # Try to fetch projects for org_b while authed as org_a (non-superuser)
            resp = client.get(f"/api/integrations/project-ops/projects?organization_id={org_b.id}", headers=headers)
            assert resp.status_code == 403
        finally:
            db.close()


class TestProjectLinkingSchema:
    """Meeting session project linking fields."""

    def test_session_has_project_fields(self, client):
        """project_id is now TEXT (UUID for project-ops); migration 010
        widened it from INT for cross-app federation."""
        _, _, _, SessionLocal, RecordingSession, _, _ = _current_models()
        db = SessionLocal()
        try:
            user = _seed_user(db, "proj_user", "test123", "proj@test.com", is_superuser=True)
            org = _seed_org(db, "Project Org", "proj-org")
            _seed_membership(db, user.id, org.id, "user")

            project_uuid = "17004202-cfe1-4b6e-833e-1290e4367b21"
            session = RecordingSession(
                session_id=str(uuid.uuid4()),
                name="Project Meeting",
                status="active",
                organization_id=org.id,
                project_app="project-ops",
                project_id=project_uuid,
                project_slug="PROJ-2026-000067",
                duration=30.0,
            )
            db.add(session)
            db.commit()
            db.refresh(session)

            assert session.project_app == "project-ops"
            assert session.project_id == project_uuid
            assert session.project_slug == "PROJ-2026-000067"
        finally:
            db.rollback()
            db.close()
