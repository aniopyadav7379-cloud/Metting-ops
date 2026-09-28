"""Tests for admin pause/resume/cancel and cross-org visibility (B-import.4).

7 tests:
  1. Admin can pause a processing job; queued files stop dequeuing.
  2. Admin can resume a paused job; remaining files re-enqueue.
  3. Non-admin cannot access admin endpoints (403).
  4. Admin can list all jobs across orgs (cross-org visible for admin only).
  5. Cross-org isolation: non-admin user A can't see user B's jobs.
  6. Admin cancel bypass: admin can cancel any job in any org.
  7. Admin get job detail returns full file list across orgs.
"""

from __future__ import annotations

import uuid

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import BulkImportFile, BulkImportJob, RecordingSession
    return (
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        RecordingSession,
        BulkImportJob,
        BulkImportFile,
    )


def _seed_user(db, username, password, email, is_superuser=False):
    _, User, _, _, _, _, _ = _models()
    user = User(
        email=email,
        username=username,
        hashed_password=get_password_hash(password),
        is_active=True,
        is_verified=True,
        tier="enterprise",  # paid tier: server-processing endpoints are gated (v3.0.0)
        is_superuser=is_superuser,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login(client, username, password):
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def two_orgs(client):
    """Two orgs, one admin user, one regular user per org."""
    Organization, _, UserOrganization, SessionLocal, _, _, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        org_a = Organization(
            name=f"Org Alpha {suffix}", slug=f"alpha-{suffix}", is_active=True,
            plan="enterprise",  # billing-1: paid workspace matches the enterprise users
        )
        org_b = Organization(
            name=f"Org Bravo {suffix}", slug=f"bravo-{suffix}", is_active=True,
            plan="enterprise",  # billing-1: paid workspace matches the enterprise users
        )
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        # Admin user (belongs to both orgs, admin role)
        admin = _seed_user(
            db, f"admin_{suffix}", "Password123",
            f"admin_{suffix}@example.com", is_superuser=True,
        )
        # Regular user in org A
        u_a = _seed_user(
            db, f"usera_{suffix}", "Password123", f"a_{suffix}@example.com",
        )
        # Regular user in org B
        u_b = _seed_user(
            db, f"userb_{suffix}", "Password123", f"b_{suffix}@example.com",
        )

        db.add_all([
            UserOrganization(
                user_id=admin.id, organization_id=org_a.id, role="admin",
            ),
            UserOrganization(
                user_id=admin.id, organization_id=org_b.id, role="admin",
            ),
            UserOrganization(
                user_id=u_a.id, organization_id=org_a.id, role="user",
            ),
            UserOrganization(
                user_id=u_b.id, organization_id=org_b.id, role="user",
            ),
        ])
        db.commit()

        ctx = {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "admin_headers": _login(client, admin.username, "Password123"),
            "user_a_headers": _login(client, u_a.username, "Password123"),
            "user_b_headers": _login(client, u_b.username, "Password123"),
            "admin_user": admin,
            "user_a": u_a,
            "user_b": u_b,
        }
    finally:
        db.close()
    return ctx


def _create_job(client, headers):
    resp = client.post("/api/import/jobs", headers=headers)
    assert resp.status_code == 200
    return resp.json()["job_id"]


@pytest.fixture(autouse=True)
def _stub_queue(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "services.bulk_import_queue.bulk_import_queue.submit", _noop,
    )


def _fake_audio_bytes(label="audio"):
    body = (label + ":").encode() * 1024
    return body[:4096]


NOTES_FILENAME = "notes__2025-03-15_143000__Quarterly_Planning_Sync.m4a"


def _add_file_to_job(client, headers, job_id, filename=NOTES_FILENAME):
    audio_bytes = _fake_audio_bytes(uuid.uuid4().hex)
    return client.post(
        f"/api/import/jobs/{job_id}/files",
        headers=headers,
        files={"audio": (filename, audio_bytes, "audio/mp4")},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_admin_pause_resume_job(client, two_orgs):
    """Admin can pause a processing job; then resume it."""
    admin_h = two_orgs["admin_headers"]
    job_id = _create_job(client, two_orgs["user_a_headers"])

    _add_file_to_job(client, two_orgs["user_a_headers"], job_id)
    _add_file_to_job(client, two_orgs["user_a_headers"], job_id)

    # Admin pause
    resp = client.post(f"/api/import/admin/jobs/{job_id}/pause", headers=admin_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "paused"

    # Verify job status changed
    resp = client.get(f"/api/import/jobs/{job_id}", headers=two_orgs["user_a_headers"])
    assert resp.json()["status"] == "paused"

    # Admin resume
    resp = client.post(f"/api/import/admin/jobs/{job_id}/resume", headers=admin_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "processing"

    resp = client.get(f"/api/import/jobs/{job_id}", headers=two_orgs["user_a_headers"])
    assert resp.json()["status"] == "processing"


def test_non_admin_cannot_access_admin_endpoints(client, two_orgs):
    """Non-admin user gets 403 on admin endpoints."""
    user_h = two_orgs["user_a_headers"]
    job_id = _create_job(client, two_orgs["user_a_headers"])

    endpoints = [
        "GET", f"/api/import/admin/jobs",
        "POST", f"/api/import/admin/jobs/{job_id}/pause",
        "POST", f"/api/import/admin/jobs/{job_id}/resume",
        "POST", f"/api/import/admin/jobs/{job_id}/cancel",
        "GET", f"/api/import/admin/jobs/{job_id}",
    ]

    for i in range(0, len(endpoints), 2):
        method, path = endpoints[i], endpoints[i + 1]
        if method == "GET":
            resp = client.get(path, headers=user_h)
        else:
            resp = client.post(path, headers=user_h)
        assert resp.status_code == 403, (
            f"Expected 403 for {method} {path}, got {resp.status_code}"
        )


def test_admin_list_cross_org_jobs(client, two_orgs):
    """Admin can see jobs across both orgs."""
    admin_h = two_orgs["admin_headers"]

    # Create job in org A
    job_a = _create_job(client, two_orgs["user_a_headers"])
    _add_file_to_job(client, two_orgs["user_a_headers"], job_a)

    # Create job in org B
    job_b = _create_job(client, two_orgs["user_b_headers"])
    _add_file_to_job(client, two_orgs["user_b_headers"], job_b)

    # Admin list
    resp = client.get("/api/import/admin/jobs", headers=admin_h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Should see at least 2 jobs (there may be others from other tests)
    job_ids = [j["job_id"] for j in body["jobs"]]
    assert job_a in job_ids, f"Job from org A not visible to admin: {job_ids}"
    assert job_b in job_ids, f"Job from org B not visible to admin: {job_ids}"


def test_cross_org_isolation_non_admin(client, two_orgs):
    """Non-admin user A cannot see user B's job (404)."""
    job_a = _create_job(client, two_orgs["user_a_headers"])
    job_b = _create_job(client, two_orgs["user_b_headers"])

    # User B should get 404 on user A's job (cross-org)
    resp = client.get(f"/api/import/jobs/{job_a}", headers=two_orgs["user_b_headers"])
    assert resp.status_code == 404

    # User A should get 200 on their own job
    resp = client.get(f"/api/import/jobs/{job_a}", headers=two_orgs["user_a_headers"])
    assert resp.status_code == 200


def test_admin_cancel_any_job(client, two_orgs):
    """Admin can cancel a job in any org regardless of ownership."""
    admin_h = two_orgs["admin_headers"]
    job_id = _create_job(client, two_orgs["user_b_headers"])

    _add_file_to_job(client, two_orgs["user_b_headers"], job_id)

    resp = client.post(
        f"/api/import/admin/jobs/{job_id}/cancel", headers=admin_h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"

    # Verify via regular endpoint
    resp = client.get(
        f"/api/import/jobs/{job_id}", headers=two_orgs["user_b_headers"],
    )
    assert resp.json()["status"] == "cancelled"


def test_admin_get_job_detail_across_orgs(client, two_orgs):
    """Admin can get full detail of a job owned by user in another org."""
    admin_h = two_orgs["admin_headers"]
    job_id = _create_job(client, two_orgs["user_b_headers"])

    _add_file_to_job(client, two_orgs["user_b_headers"], job_id)

    resp = client.get(
        f"/api/import/admin/jobs/{job_id}", headers=admin_h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job_id
    assert len(body["files"]) == 1
    assert body["files"][0]["status"] == "queued"


def test_admin_job_filter_by_status(client, two_orgs):
    """Admin list can filter by status."""
    admin_h = two_orgs["admin_headers"]

    # Create a job
    _create_job(client, two_orgs["user_a_headers"])

    resp = client.get(
        "/api/import/admin/jobs?status=queued", headers=admin_h,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert all(j["status"] == "queued" for j in body["jobs"])
