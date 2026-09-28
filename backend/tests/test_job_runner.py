"""v3.18.3 background-jobs: tests for the generic job runner.

Covers:
  1. enqueue_job hands off to arq with the right function_name + args.
  2. get_job_status maps arq JobStatus enums to our public strings.
  3. Idempotency window: identical (function, args) inside 5 min returns
     the same job_id without re-enqueueing on arq.
  4. /api/jobs/{id} returns 404 when arq reports not_found.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _flush_idempotency_cache():
    """Reset the in-process idempotency cache between tests so one
    test's enqueue doesn't poison the next."""
    from services.job_runner import reset_idempotency_cache_for_tests
    reset_idempotency_cache_for_tests()
    yield
    reset_idempotency_cache_for_tests()


@pytest.mark.asyncio
async def test_enqueue_job_calls_arq_with_function_name():
    """enqueue_job translates (function_name, *args, **kwargs) into the
    arq pool's enqueue_job call and returns the job_id."""
    from services import job_runner

    mock_job = MagicMock()
    mock_job.job_id = "arq-test-1"
    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
        result = await job_runner.enqueue_job("finalize_session_job", 42, user_id=7)

    assert result == "arq-test-1"
    mock_pool.enqueue_job.assert_called_once()
    args, kwargs = mock_pool.enqueue_job.call_args
    assert args[0] == "finalize_session_job"
    assert args[1] == 42
    assert kwargs == {
        "user_id": 7,
        "_queue_name": job_runner.INTERACTIVE_QUEUE_NAME,
    }


@pytest.mark.asyncio
async def test_enqueue_job_stamps_owner_without_forwarding_metadata():
    from services import job_runner

    mock_job = MagicMock(job_id="arq-owned-1")
    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
        result = await job_runner.enqueue_job(
            "finalize_session_job",
            42,
            owner_user_id=7,
            owner_org_id=9,
        )

    assert result == "arq-owned-1"
    mock_pool.enqueue_job.assert_awaited_once_with(
        "finalize_session_job",
        42,
        _queue_name=job_runner.INTERACTIVE_QUEUE_NAME,
    )
    mock_pool.setex.assert_awaited_once()
    owner_key, ttl, value = mock_pool.setex.await_args.args
    assert owner_key.endswith("arq-owned-1")
    assert ttl >= 3600
    assert value == '{"user_id": 7, "org_id": 9}'


@pytest.mark.asyncio
async def test_enqueue_job_propagates_bound_request_id():
    from middleware.request_context import bind_request_id, request_id_var
    from services import job_runner

    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=MagicMock(job_id="trace-job"))
    token = bind_request_id("request-abc")
    try:
        with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
            await job_runner.enqueue_job("finalize_session_job", 42)
    finally:
        request_id_var.reset(token)

    mock_pool.enqueue_job.assert_awaited_once_with(
        "finalize_session_job",
        42,
        _queue_name=job_runner.INTERACTIVE_QUEUE_NAME,
        request_id="request-abc",
    )


@pytest.mark.asyncio
async def test_enqueue_job_idempotency_window_dedupes_same_args():
    """Inside the 5-minute window, a second enqueue with identical args
    returns the cached job_id and does NOT call arq again."""
    from services import job_runner

    mock_job = MagicMock()
    mock_job.job_id = "arq-test-dedupe"
    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
        first = await job_runner.enqueue_job("finalize_session_job", 99, user_id=1)
        second = await job_runner.enqueue_job("finalize_session_job", 99, user_id=1)

    assert first == second == "arq-test-dedupe"
    # Only one actual arq enqueue.
    assert mock_pool.enqueue_job.call_count == 1


@pytest.mark.asyncio
async def test_enqueue_job_different_args_does_not_dedupe():
    """Different args for the same function get separate job_ids — the
    dedup key is (function, args, kwargs)."""
    from services import job_runner

    mock_pool = AsyncMock()
    side_effects = [MagicMock(job_id="arq-job-A"), MagicMock(job_id="arq-job-B")]
    mock_pool.enqueue_job = AsyncMock(side_effect=side_effects)

    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
        a = await job_runner.enqueue_job("finalize_session_job", 1)
        b = await job_runner.enqueue_job("finalize_session_job", 2)

    assert a == "arq-job-A"
    assert b == "arq-job-B"
    assert mock_pool.enqueue_job.call_count == 2


@pytest.mark.asyncio
async def test_get_job_status_completed_returns_result():
    """A completed job exposes its return value under `result`."""
    from arq.jobs import JobStatus
    from services import job_runner

    mock_pool = AsyncMock()
    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
        with patch.object(job_runner, "Job") as MockJob:
            instance = MockJob.return_value
            instance.info = AsyncMock(return_value=MagicMock(
                enqueue_time=None, start_time=None, finish_time=None,
            ))
            instance.status = AsyncMock(return_value=JobStatus.complete)
            instance.result = AsyncMock(return_value={"status": "completed", "session_pk": 7})

            payload = await job_runner.get_job_status("arq-finished")

    assert payload["job_id"] == "arq-finished"
    assert payload["status"] == "completed"
    assert payload["result"] == {"status": "completed", "session_pk": 7}
    assert payload["error"] is None


@pytest.mark.asyncio
async def test_get_job_status_running_returns_running():
    """A running job maps in_progress -> 'running' with no result yet."""
    from arq.jobs import JobStatus
    from services import job_runner

    mock_pool = AsyncMock()
    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
        with patch.object(job_runner, "Job") as MockJob:
            instance = MockJob.return_value
            instance.info = AsyncMock(return_value=MagicMock(
                enqueue_time=None, start_time=None, finish_time=None,
            ))
            instance.status = AsyncMock(return_value=JobStatus.in_progress)
            instance.result = AsyncMock(return_value=None)

            payload = await job_runner.get_job_status("arq-running")

    assert payload["status"] == "running"
    assert payload["result"] is None


@pytest.mark.asyncio
async def test_get_job_status_not_found_maps_to_not_found():
    """When arq reports JobStatus.not_found, our wrapper surfaces 'not_found'
    so the HTTP layer can 404."""
    from arq.jobs import JobStatus
    from services import job_runner

    mock_pool = AsyncMock()
    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
        with patch.object(job_runner, "Job") as MockJob:
            instance = MockJob.return_value
            instance.info = AsyncMock(return_value=None)
            instance.status = AsyncMock(return_value=JobStatus.not_found)

            payload = await job_runner.get_job_status("never-existed")

    assert payload["status"] == "not_found"


@pytest.mark.asyncio
async def test_get_job_status_completed_with_worker_error_marks_failed():
    """When arq says complete but `result()` raises (worker threw), we
    flip status to 'failed' and surface the error."""
    from arq.jobs import JobStatus
    from services import job_runner

    mock_pool = AsyncMock()
    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=mock_pool)):
        with patch.object(job_runner, "Job") as MockJob:
            instance = MockJob.return_value
            instance.info = AsyncMock(return_value=MagicMock(
                enqueue_time=None, start_time=None, finish_time=None,
            ))
            instance.status = AsyncMock(return_value=JobStatus.complete)
            instance.result = AsyncMock(side_effect=ValueError("worker crashed"))

            payload = await job_runner.get_job_status("arq-failed")

    assert payload["status"] == "failed"
    assert "worker crashed" in (payload["error"] or "")


def test_jobs_endpoint_returns_404_for_unknown_job(client):
    """/api/jobs/{id} should 404 when the underlying job_runner reports
    not_found."""
    # Seed an admin user — the endpoint requires authentication.
    from auth.utils import get_password_hash
    from auth.models import User
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == "jobs_user").first()
        if not existing:
            existing = User(
                email="jobs@meeting-ops.local",
                username="jobs_user",
                hashed_password=get_password_hash("admin123"),
                is_active=True, is_verified=True, is_superuser=True,
                tier="pro",
            )
            db.add(existing)
        else:
            existing.is_superuser = True
        db.commit()
    finally:
        db.close()

    login = client.post(
        "/api/auth/login",
        data={"username": "jobs_user", "password": "admin123"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    async def fake_status(job_id):
        return {"job_id": job_id, "status": "not_found", "result": None, "error": None,
                "queued_at": None, "started_at": None, "finished_at": None}

    with patch("api.jobs.get_job_owner", AsyncMock(return_value=None)), \
         patch("api.jobs.get_job_status", side_effect=fake_status):
        resp = client.get(
            "/api/jobs/does-not-exist",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 404


def test_jobs_endpoint_returns_status_for_known_job(client):
    """/api/jobs/{id} returns the job_runner payload as-is when status
    is anything other than not_found."""
    from auth.utils import get_password_hash
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "jobs_user2").first()
        if not user:
            user = User(
                email="jobs2@meeting-ops.local",
                username="jobs_user2",
                hashed_password=get_password_hash("admin123"),
                is_active=True, is_verified=True, is_superuser=False,
                tier="pro",
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        org = db.query(Organization).filter(Organization.slug == "jobs-org").first()
        if not org:
            org = Organization(name="Jobs Org", slug="jobs-org", is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
        if not db.query(UserOrganization).filter(
            UserOrganization.user_id == user.id,
            UserOrganization.organization_id == org.id,
        ).first():
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
            db.commit()
        user_id, org_id = user.id, org.id
    finally:
        db.close()

    login = client.post(
        "/api/auth/login",
        data={"username": "jobs_user2", "password": "admin123"},
    )
    token = login.json()["access_token"]

    payload = {
        "job_id": "abc-123",
        "status": "running",
        "result": None,
        "error": None,
        "queued_at": "2026-05-29T12:00:00",
        "started_at": "2026-05-29T12:00:01",
        "finished_at": None,
    }

    async def fake_status(job_id):
        return payload

    with patch(
        "api.jobs.get_job_owner",
        AsyncMock(return_value={"user_id": user_id, "org_id": org_id}),
    ), patch("api.jobs.get_job_status", side_effect=fake_status):
        resp = client.get(
            "/api/jobs/abc-123",
            headers={
                "Authorization": f"Bearer {token}",
                "X-MeetingOps-Org": "jobs-org",
            },
        )

    assert resp.status_code == 200
    assert resp.json() == payload


def test_jobs_endpoint_rejects_cross_org_owner(client):
    login = client.post(
        "/api/auth/login",
        data={"username": "jobs_user2", "password": "admin123"},
    )
    token = login.json()["access_token"]

    with patch(
        "api.jobs.get_job_owner",
        AsyncMock(return_value={"user_id": -1, "org_id": -1}),
    ), patch("api.jobs.get_job_status", AsyncMock()) as status_mock:
        response = client.get(
            "/api/jobs/foreign-job",
            headers={
                "Authorization": f"Bearer {token}",
                "X-MeetingOps-Org": "jobs-org",
            },
        )

    assert response.status_code == 404
    status_mock.assert_not_awaited()
