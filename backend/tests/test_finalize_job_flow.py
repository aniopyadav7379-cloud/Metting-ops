"""v3.18.3 background-jobs: tests for the always-on finalize flow.

Covers:
  1. POST /finalize returns 202 with job_id + status_url when arq is up.
  2. The session row gets `processing_job_id` stamped to the arq job_id.
  3. GET /api/jobs/{id} reports the job state without 500ing.
  4. The worker function (`finalize_session_job`) drift-checks on entry
     so a stale arq job doesn't overwrite a fresher one.
"""

from __future__ import annotations

import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.utils import get_password_hash


def _seed_pro_user_and_org(slug, username):
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug, slug=slug, is_active=True, plan="pro")  # billing-1: paid workspace matches the pro user
            db.add(org); db.commit(); db.refresh(org)
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                hashed_password=get_password_hash("admin123"),
                is_active=True, is_verified=True, is_superuser=False,
                tier="pro",
            )
            db.add(user); db.commit(); db.refresh(user)
        else:
            user.tier = "pro"
            db.commit()
        if not (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.organization_id == org.id,
            )
            .first()
        ):
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
            db.commit()
        return org.id, org.slug, user.id
    finally:
        db.close()


def _seed_always_on_session(org_id, user_id):
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid,
            name=f"finalize-job-{sid[:8]}",
            title=f"finalize-job-{sid[:8]}",
            meeting_type="always_on",
            mode="always_on",
            status="recording",
            duration=0.0,
            user_id=user_id,
            organization_id=org_id,
            source_type="browser_always_on",
            processing_metadata={"created_by": "test"},
        )
        db.add(s); db.commit(); db.refresh(s)
        return s.id, s.session_id
    finally:
        db.close()


def _login(client, username, org_slug):
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": "admin123"},
    )
    assert resp.status_code == 200
    return {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-MeetingOps-Org": org_slug,
    }


@pytest.fixture(autouse=True)
def _flush_idempotency_cache():
    from services.job_runner import reset_idempotency_cache_for_tests
    reset_idempotency_cache_for_tests()
    yield
    reset_idempotency_cache_for_tests()


def test_finalize_returns_202_with_job_id(client):
    """The always-on /finalize endpoint enqueues and returns 202."""
    org_id, org_slug, user_id = _seed_pro_user_and_org(
        "v3183-finalize", "v3183_finalize_user",
    )
    headers = _login(client, "v3183_finalize_user", org_slug)
    _, session_id = _seed_always_on_session(org_id, user_id)

    mock_job = MagicMock()
    mock_job.job_id = "arq-finalize-1"
    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    with patch("services.job_runner.get_arq_pool", AsyncMock(return_value=mock_pool)):
        resp = client.post(
            f"/api/recordings/sessions/{session_id}/finalize",
            headers=headers,
        )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body.get("job_id") == "arq-finalize-1"
    assert body.get("status_url") == "/api/jobs/arq-finalize-1"


def test_finalize_with_audio_routes_to_full_reprocess(client):
    """v3.42 browser<->upload parity: when the always-on session has server-side
    audio, /finalize routes through the FULL reprocess (STT+diarize+identify),
    not the light finalize — so browser recordings get upload-grade transcripts
    + named speakers. Sessions without audio (privacy/text-only) keep the light
    path (covered by test_finalize_returns_202_with_job_id)."""
    from database.database import SessionLocal
    from database.models import RecordingSession

    org_id, org_slug, user_id = _seed_pro_user_and_org("v342-parity", "v342_parity_user")
    headers = _login(client, "v342_parity_user", org_slug)
    session_pk, session_id = _seed_always_on_session(org_id, user_id)

    # Give the session server-side audio so the parity path triggers.
    db = SessionLocal()
    try:
        s = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        s.audio_file = "/tmp/fake_full_audio.wav"
        db.commit()
    finally:
        db.close()

    mock_reprocess = AsyncMock()
    with patch("workers.reprocess_workers.enqueue_reprocess", mock_reprocess):
        resp = client.post(
            f"/api/recordings/sessions/{session_id}/finalize", headers=headers,
        )

    assert resp.status_code == 202, resp.text
    assert resp.json().get("reprocessing_started") is True
    mock_reprocess.assert_awaited_once()

    db = SessionLocal()
    try:
        s = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        assert (s.processing_metadata or {}).get("reprocess_status") == "queued"
    finally:
        db.close()


def test_finalize_stamps_processing_job_id_on_session(client):
    """After enqueue, the session row carries the arq job_id so the
    worker can drift-check it later."""
    from database.database import SessionLocal
    from database.models import RecordingSession

    org_id, org_slug, user_id = _seed_pro_user_and_org(
        "v3183-stamp", "v3183_stamp_user",
    )
    headers = _login(client, "v3183_stamp_user", org_slug)
    session_pk, session_id = _seed_always_on_session(org_id, user_id)

    mock_job = MagicMock()
    mock_job.job_id = "arq-stamp-99"
    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    with patch("services.job_runner.get_arq_pool", AsyncMock(return_value=mock_pool)):
        resp = client.post(
            f"/api/recordings/sessions/{session_id}/finalize",
            headers=headers,
        )
    assert resp.status_code == 202

    db = SessionLocal()
    try:
        s = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        assert s is not None
        assert s.processing_job_id == "arq-stamp-99"
        assert s.status == "processing"
    finally:
        db.close()


def test_free_user_still_blocked_before_enqueue(client):
    """The tier gate fires BEFORE the enqueue — a free user must still
    see 403, never 202."""
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == "v3183-free").first()
        if not org:
            org = Organization(name="v3183-free", slug="v3183-free", is_active=True)
            db.add(org); db.commit(); db.refresh(org)
        user = db.query(User).filter(User.username == "v3183_free").first()
        if not user:
            user = User(
                email="v3183_free@meeting-ops.local",
                username="v3183_free",
                hashed_password=get_password_hash("admin123"),
                is_active=True, is_verified=True, is_superuser=False,
                tier="free",
            )
            db.add(user); db.commit(); db.refresh(user)
        else:
            user.tier = "free"; db.commit()
        if not (
            db.query(UserOrganization)
            .filter(UserOrganization.user_id == user.id, UserOrganization.organization_id == org.id)
            .first()
        ):
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
            db.commit()
        org_id = org.id
        user_id = user.id
    finally:
        db.close()

    _, session_id = _seed_always_on_session(org_id, user_id)
    headers = _login(client, "v3183_free", "v3183-free")

    # Even if arq is reachable, the gate has to fire first.
    mock_pool = AsyncMock()
    with patch("services.job_runner.get_arq_pool", AsyncMock(return_value=mock_pool)):
        resp = client.post(
            f"/api/recordings/sessions/{session_id}/finalize",
            headers=headers,
        )
    assert resp.status_code == 403, resp.text
    # And arq is never touched on the free path.
    mock_pool.enqueue_job.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_worker_drift_check_skips_on_mismatch():
    """When `processing_job_id` no longer matches the running worker's
    ctx job_id, the worker bails without touching summary/insights/Brigade."""
    from database.database import SessionLocal
    from database.models import RecordingSession
    from workers.finalize_workers import finalize_session_job

    org_id, _, user_id = _seed_pro_user_and_org(
        "v3183-drift", "v3183_drift_user",
    )
    session_pk, _ = _seed_always_on_session(org_id, user_id)

    # Pretend a newer job claimed the row.
    db = SessionLocal()
    try:
        s = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        s.processing_job_id = "newer-job-id"
        db.commit()
    finally:
        db.close()

    # Run with an OLDER job_id — drift check must skip.
    result = await finalize_session_job(
        {"job_id": "older-stale-job"},
        session_pk,
        user_id=user_id,
        org_id=org_id,
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "drift"


@pytest.mark.asyncio
async def test_finalize_worker_missing_session_returns_skipped():
    """If the session was deleted between enqueue and run, the worker
    doesn't crash — it returns a clean skipped status."""
    from workers.finalize_workers import finalize_session_job

    result = await finalize_session_job(
        {"job_id": "any"},
        999_999_999,  # PK that won't exist
        user_id=None,
        org_id=None,
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "session_missing"


@pytest.mark.asyncio
async def test_finalize_worker_keeps_summary_failure_retryable(monkeypatch):
    from arq import Retry
    import api.uploads as uploads_mod
    from database.database import SessionLocal
    from database.models import RecordingSession
    from services.providers.impl_llm import LLMUnavailable
    from workers.finalize_workers import finalize_session_job

    org_id, _, user_id = _seed_pro_user_and_org(
        "v3183-summary-retry", "v3183_summary_retry_user",
    )
    session_pk, _ = _seed_always_on_session(org_id, user_id)

    async def unavailable(*args, **kwargs):
        raise LLMUnavailable("gateway unavailable")

    monkeypatch.setattr(uploads_mod, "_summarize_session", unavailable)
    with pytest.raises(Retry):
        await finalize_session_job({"job_id": "summary-retry", "job_try": 1}, session_pk)
    db = SessionLocal()
    try:
        session = db.get(RecordingSession, session_pk)
        assert session.status == "processing"
        assert session.processing_metadata["needs_summary"] is True
        assert "gateway unavailable" in session.processing_metadata["summary_error"]
    finally:
        db.close()
