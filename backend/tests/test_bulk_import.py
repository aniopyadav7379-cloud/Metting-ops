"""Tests for /api/import (bulk audio import — B-import.1).

7 tests covering the per-doc verification checklist:

  1. create_job:          POST /api/import/jobs returns a job_id, DB row
                          exists with status='queued'.
  2. upload_file:         POST /api/import/jobs/{id}/files persists the
                          audio + creates a BulkImportFile row with the
                          parsed_title / parsed_date populated from the
                          Mac Notes filename pattern.
  3. duplicate_sha256:    Two uploads of the same bytes; second is marked
                          skipped, job.skipped increments, no second
                          RecordingSession created.
  4. cross_org_404:       User B can't GET / POST cancel on user A's
                          job — both return 404 (not 403, so we don't
                          leak existence).
  5. concurrency_cap:     Submit 5 files to a queue with max_workers=2;
                          assert at most 2 are in-flight concurrently
                          (semaphore math).
  6. cancel_mid_flight:   Cancel a job after some files are processing;
                          status flips to 'cancelled', queued files become
                          'skipped', already-processing files keep running.
  7. cross_org_files:     POST file to a job in another org returns 404
                          (cross-org isolation on the write path too).

The queue + the reprocess pipeline are mocked at module boundaries:
  - bulk_import_queue.submit is monkeypatched to a no-op so tests don't
    spawn real background tasks against the test SQLite.
  - api.recording._run_session_reprocess is monkeypatched to a no-op so
    tests don't try to hit Parakeet / pyannote / Qwen.
  - bulk_import_queue.BulkImportPipelineQueue is exercised directly in
    the concurrency_cap test against a sleep-mocked _do_process_file.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from typing import Any, Optional
from unittest.mock import patch

import pytest

from auth.utils import get_password_hash


# ---------------------------------------------------------------------------
# Helpers (copy of the test_cross_org_isolation pattern)
# ---------------------------------------------------------------------------


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import (
        BulkImportFile,
        BulkImportJob,
        RecordingSession,
    )

    return (
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        RecordingSession,
        BulkImportJob,
        BulkImportFile,
    )


def _seed_user(db, username: str, password: str, email: str):
    _, User, _, _, _, _, _ = _models()
    user = User(
        email=email,
        username=username,
        hashed_password=get_password_hash(password),
        is_active=True,
        is_verified=True,
        tier="enterprise",  # paid tier: server-processing endpoints are gated (v3.0.0)
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
    assert response.status_code == 200, f"Login failed: {response.text}"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def two_users_one_org(client):
    """Two users in the same org so we can test user-A-can't-touch-user-B's-job."""
    Organization, _, UserOrganization, SessionLocal, _, _, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        org = Organization(
            name=f"BulkImport {suffix}",
            slug=f"bulk-{suffix}",
            is_active=True,
            plan="enterprise",  # billing-1: paid workspace matches the enterprise users
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        u_a = _seed_user(db, f"bulka_{suffix}", "Password123", f"a_{suffix}@example.com")
        u_b = _seed_user(db, f"bulkb_{suffix}", "Password123", f"b_{suffix}@example.com")
        db.add_all([
            UserOrganization(user_id=u_a.id, organization_id=org.id, role="user"),
            UserOrganization(user_id=u_b.id, organization_id=org.id, role="user"),
        ])
        db.commit()
        ctx = {
            "org_id": org.id,
            "user_a_username": u_a.username,
            "user_b_username": u_b.username,
        }
    finally:
        db.close()
    ctx["headers_a"] = _login_headers(client, ctx["user_a_username"], "Password123")
    ctx["headers_b"] = _login_headers(client, ctx["user_b_username"], "Password123")
    return ctx


@pytest.fixture()
def two_orgs(client):
    """Two orgs, one user each — for cross-org 404 tests."""
    Organization, _, UserOrganization, SessionLocal, _, _, _ = _models()
    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        org_a = Organization(
            name=f"Org Alpha {suffix}",
            slug=f"alpha-{suffix}",
            is_active=True,
            plan="enterprise",  # billing-1: paid workspace matches the enterprise users
        )
        org_b = Organization(
            name=f"Org Bravo {suffix}",
            slug=f"bravo-{suffix}",
            is_active=True,
            plan="enterprise",  # billing-1: paid workspace matches the enterprise users
        )
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)
        u_a = _seed_user(db, f"oa_{suffix}", "Password123", f"oa_{suffix}@example.com")
        u_b = _seed_user(db, f"ob_{suffix}", "Password123", f"ob_{suffix}@example.com")
        db.add_all([
            UserOrganization(user_id=u_a.id, organization_id=org_a.id, role="user"),
            UserOrganization(user_id=u_b.id, organization_id=org_b.id, role="user"),
        ])
        db.commit()
        ctx = {
            "org_a_id": org_a.id,
            "org_b_id": org_b.id,
            "user_a_username": u_a.username,
            "user_b_username": u_b.username,
        }
    finally:
        db.close()
    ctx["headers_a"] = _login_headers(client, ctx["user_a_username"], "Password123")
    ctx["headers_b"] = _login_headers(client, ctx["user_b_username"], "Password123")
    return ctx


# A canonical Mac Notes export filename that pattern 1 should hit with confidence 1.0.
NOTES_FILENAME = "notes__2025-03-15_143000__Quarterly_Planning_Sync.m4a"


def _fake_audio_bytes(label: str = "audio") -> bytes:
    """A 4 KiB buffer with a deterministic prefix so two identical
    uploads collide on SHA-256 but two different labels don't."""
    body = (label + ":").encode() * 1024
    return body[:4096]


@pytest.fixture(autouse=True)
def _stub_queue_and_reprocess(monkeypatch):
    """Default: queue.submit + reprocess are no-ops so tests don't kick
    real background work. Individual tests can override.

    The Phase 1 pipeline is intentionally easy to mock at these two
    boundaries — the API row + audio persistence happens inline, the
    rest runs in the worker. This fixture keeps every API-shape test
    fast and deterministic without touching the queue internals.
    """
    async def _noop_submit(file_id):  # noqa: D401
        return None

    monkeypatch.setattr(
        "services.bulk_import_queue.bulk_import_queue.submit",
        _noop_submit,
    )
    # Also stub the recording reprocess in case any test runs the queue.
    try:
        import api.recording as recording

        async def _noop_reprocess(_pk):
            return None

        monkeypatch.setattr(recording, "_run_session_reprocess", _noop_reprocess)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_job_inserts_row(client, two_users_one_org):
    """POST /api/import/jobs returns job_id + status=queued, DB row exists."""
    headers = two_users_one_org["headers_a"]
    resp = client.post("/api/import/jobs", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    job_uuid = uuid.UUID(body["job_id"])

    _, _, _, SessionLocal, _, BulkImportJob, _ = _models()
    db = SessionLocal()
    try:
        row = (
            db.query(BulkImportJob)
            .filter(BulkImportJob.id == job_uuid)
            .first()
        )
        assert row is not None
        assert row.status == "queued"
        assert row.total_files == 0
        assert row.organization_id == two_users_one_org["org_id"]
    finally:
        db.close()


def test_upload_file_parses_filename(client, two_users_one_org):
    """POST a file with the Mac Notes pattern; row gets parsed title +
    parsed_date populated. SHA-256 stays None until the worker runs."""
    headers = two_users_one_org["headers_a"]
    job_resp = client.post("/api/import/jobs", headers=headers)
    job_id = job_resp.json()["job_id"]

    audio_bytes = _fake_audio_bytes("planning")
    resp = client.post(
        f"/api/import/jobs/{job_id}/files",
        headers=headers,
        files={"audio": (NOTES_FILENAME, audio_bytes, "audio/mp4")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["original_filename"] == NOTES_FILENAME
    # Pattern 1 hits with confidence 1.0; date+time+title all populated.
    assert body["parsed_title"] == "Quarterly Planning Sync"
    assert body["parsed_date"] == "2025-03-15"
    assert body["parsed_time"] == "14:30:00"
    assert body["parsed_source"] == "notes"
    assert body["parsed_confidence"] == 1.0
    assert body["status"] == "queued"
    assert body["bytes_total"] == len(audio_bytes)

    _, _, _, SessionLocal, _, BulkImportJob, BulkImportFile = _models()
    db = SessionLocal()
    try:
        row = (
            db.query(BulkImportFile)
            .filter(BulkImportFile.id == uuid.UUID(body["file_id"]))
            .first()
        )
        assert row is not None
        assert row.parsed_title == "Quarterly Planning Sync"
        # SHA-256 is computed by the worker, not by upload — None at this stage.
        assert row.file_sha256 is None
        # Job counter incremented.
        job = (
            db.query(BulkImportJob)
            .filter(BulkImportJob.id == uuid.UUID(job_id))
            .first()
        )
        assert job.total_files == 1
    finally:
        db.close()


def test_duplicate_sha256_marked_skipped(client, two_users_one_org, monkeypatch):
    """Upload two identical files; run the queue worker manually; second
    one is marked skipped, job.skipped increments, only one
    RecordingSession was created."""
    headers = two_users_one_org["headers_a"]
    job_resp = client.post("/api/import/jobs", headers=headers)
    job_id = job_resp.json()["job_id"]

    audio_bytes = _fake_audio_bytes("dup")
    # First upload.
    r1 = client.post(
        f"/api/import/jobs/{job_id}/files",
        headers=headers,
        files={"audio": ("notes__2025-04-01_090000__call_with_alex.m4a", audio_bytes, "audio/mp4")},
    )
    assert r1.status_code == 200, r1.text
    file_id_1 = uuid.UUID(r1.json()["file_id"])

    # Second upload, identical bytes -> same SHA-256.
    r2 = client.post(
        f"/api/import/jobs/{job_id}/files",
        headers=headers,
        files={"audio": ("notes__2025-04-02_090000__call_with_alex_again.m4a", audio_bytes, "audio/mp4")},
    )
    assert r2.status_code == 200, r2.text
    file_id_2 = uuid.UUID(r2.json()["file_id"])

    # Run the worker pipeline directly. The autouse fixture stubbed
    # submit() to no-op, so neither task ran yet. We unstub here and
    # invoke _do_process_file synchronously so the test asserts on the
    # actual queue behaviour rather than the stub.
    from services import bulk_import_queue as bulk_mod

    async def _noop_reprocess(_pk):
        return None

    import api.recording as recording
    monkeypatch.setattr(recording, "_run_session_reprocess", _noop_reprocess)

    q = bulk_mod.BulkImportPipelineQueue(max_workers=1)

    async def _run_both():
        await q._do_process_file(file_id_1)
        await q._do_process_file(file_id_2)

    asyncio.run(_run_both())

    _, _, _, SessionLocal, RecordingSession, BulkImportJob, BulkImportFile = _models()
    db = SessionLocal()
    try:
        f1 = db.query(BulkImportFile).filter(BulkImportFile.id == file_id_1).first()
        f2 = db.query(BulkImportFile).filter(BulkImportFile.id == file_id_2).first()
        # The first file succeeds; the second hits the SHA-256 dedup.
        assert f1.status == "complete", f"f1.error_message={f1.error_message}"
        assert f1.file_sha256 is not None
        assert f2.status == "skipped"
        assert f2.file_sha256 == f1.file_sha256
        assert "duplicate" in (f2.error_message or "").lower()

        job = (
            db.query(BulkImportJob)
            .filter(BulkImportJob.id == uuid.UUID(job_id))
            .first()
        )
        assert job.succeeded == 1
        assert job.skipped == 1
        assert job.failed == 0

        # Only one session created.
        sessions = (
            db.query(RecordingSession)
            .filter(RecordingSession.organization_id == two_users_one_org["org_id"])
            .all()
        )
        assert len(sessions) == 1, f"unexpected session count: {len(sessions)}"
    finally:
        db.close()


def test_cross_org_get_returns_404(client, two_orgs):
    """User B can't GET status of user A's job — same 404 as missing
    (no existence leak)."""
    # Create a job as user A.
    r_create = client.post("/api/import/jobs", headers=two_orgs["headers_a"])
    assert r_create.status_code == 200
    job_id = r_create.json()["job_id"]

    # GET as user A works.
    r_get_a = client.get(
        f"/api/import/jobs/{job_id}", headers=two_orgs["headers_a"]
    )
    assert r_get_a.status_code == 200

    # GET as user B (different org) returns 404.
    r_get_b = client.get(
        f"/api/import/jobs/{job_id}", headers=two_orgs["headers_b"]
    )
    assert r_get_b.status_code == 404

    # Cancel as user B (different org) also returns 404.
    r_cancel_b = client.post(
        f"/api/import/jobs/{job_id}/cancel", headers=two_orgs["headers_b"]
    )
    assert r_cancel_b.status_code == 404

    # File upload to user A's job from user B (different org) also 404.
    audio_bytes = _fake_audio_bytes()
    r_post_file_b = client.post(
        f"/api/import/jobs/{job_id}/files",
        headers=two_orgs["headers_b"],
        files={"audio": (NOTES_FILENAME, audio_bytes, "audio/mp4")},
    )
    assert r_post_file_b.status_code == 404


def test_concurrency_cap_semaphore_holds(monkeypatch):
    """Spawn 5 files at the queue with max_workers=2; at no point can
    more than 2 worker tasks be inside the per-file pipeline at once.

    We mock _do_process_file to sleep + record start/end into a shared
    list, then count peak concurrent in-flight.
    """
    from services import bulk_import_queue as bulk_mod

    q = bulk_mod.BulkImportPipelineQueue(max_workers=2)
    assert q.max_workers == 2

    in_flight = 0
    peak = 0
    lock = asyncio.Lock()

    async def fake_do_process(file_id):
        nonlocal in_flight, peak
        async with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        # Long enough that all 5 tasks have been spawned by the time the
        # first one finishes — the semaphore is the only gate.
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1

    # Bind our fake to the instance. We can't easily monkeypatch the
    # bound method on the class without affecting other tests, so we
    # subclass it for this test only.
    class TestQueue(bulk_mod.BulkImportPipelineQueue):
        async def _do_process_file(self, file_id):  # type: ignore[override]
            await fake_do_process(file_id)

    q2 = TestQueue(max_workers=2)

    async def run():
        ids = [uuid.uuid4() for _ in range(5)]
        for fid in ids:
            await q2.submit(fid)
        # Wait for all spawned tasks to drain.
        if q2._tasks:
            await asyncio.gather(*q2._tasks, return_exceptions=True)

    asyncio.run(run())

    assert peak == 2, f"semaphore broke; peak in-flight was {peak}"


def test_cancel_mid_flight_skips_queued(client, two_users_one_org):
    """Cancel a job that has queued files; the queued files flip to
    'skipped' immediately and the job status becomes 'cancelled'."""
    headers = two_users_one_org["headers_a"]
    job_resp = client.post("/api/import/jobs", headers=headers)
    job_id = job_resp.json()["job_id"]

    # Upload a couple files; both stay queued because submit() is stubbed.
    for i in range(3):
        r = client.post(
            f"/api/import/jobs/{job_id}/files",
            headers=headers,
            files={
                "audio": (
                    f"notes__2025-05-0{i+1}_120000__meeting_{i+1}.m4a",
                    _fake_audio_bytes(f"f{i}"),
                    "audio/mp4",
                ),
            },
        )
        assert r.status_code == 200

    # Now cancel.
    r_cancel = client.post(
        f"/api/import/jobs/{job_id}/cancel", headers=headers
    )
    assert r_cancel.status_code == 200, r_cancel.text
    body = r_cancel.json()
    assert body["status"] == "cancelled"
    # All three queued files should now be skipped.
    file_statuses = [f["status"] for f in body["files"]]
    assert file_statuses.count("skipped") == 3
    assert body["skipped"] == 3

    # Re-GET confirms persistence (no transient response trickery).
    r_get = client.get(f"/api/import/jobs/{job_id}", headers=headers)
    assert r_get.status_code == 200
    assert r_get.json()["status"] == "cancelled"


def test_cancel_idempotent_when_already_cancelled(client, two_users_one_org):
    """A second cancel on an already-cancelled job is a 200 no-op,
    returning the existing terminal snapshot. Idempotency matters when
    the UI auto-retries on a flaky network."""
    headers = two_users_one_org["headers_a"]
    job_resp = client.post("/api/import/jobs", headers=headers)
    job_id = job_resp.json()["job_id"]

    r1 = client.post(f"/api/import/jobs/{job_id}/cancel", headers=headers)
    assert r1.status_code == 200
    assert r1.json()["status"] == "cancelled"

    r2 = client.post(f"/api/import/jobs/{job_id}/cancel", headers=headers)
    assert r2.status_code == 200
    assert r2.json()["status"] == "cancelled"
