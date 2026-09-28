"""v3.18.3 background-jobs: tests for the digest generation flow.

Covers:
  1. Cached digest read remains 200 + the legacy DigestResponse shape
     (no enqueue, any tier can read).
  2. Cache-miss + pro tier enqueues + returns 202 with job_id.
  3. Free tier still 403s before enqueue (tier gate fires first).
  4. The worker delegates to `_generate_digest` and drift-checks against
     `generation_job_id`.
"""

from __future__ import annotations

import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.utils import get_password_hash


def _seed_user(slug, username, tier):
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal

    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug, slug=slug, is_active=True)
            db.add(org); db.commit(); db.refresh(org)
        # billing-1: align the org plan with the seeded user tier (server
        # compute now also requires the ACTIVE org's plan to cover the feature).
        org.plan = (
            "enterprise" if tier == "enterprise"
            else "free" if (tier or "free") == "free"
            else "pro"
        )
        db.commit()
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                hashed_password=get_password_hash("admin123"),
                is_active=True, is_verified=True, is_superuser=False,
                tier=tier,
            )
            db.add(user); db.commit(); db.refresh(user)
        else:
            user.tier = tier
            db.commit()
        if not (
            db.query(UserOrganization)
            .filter(UserOrganization.user_id == user.id, UserOrganization.organization_id == org.id)
            .first()
        ):
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
            db.commit()
        return org.id, org.slug, user.id
    finally:
        db.close()


def _seed_completed_session(org_id, user_id):
    from database.database import SessionLocal
    from database.models import RecordingSession

    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid,
            name=f"digest-{sid[:8]}",
            title=f"digest-{sid[:8]}",
            meeting_type="standard",
            status="completed",
            duration=120.0,
            user_id=user_id,
            organization_id=org_id,
            summary="Test summary for digest aggregation.",
            processing_metadata={"created_by": "test"},
        )
        db.add(s); db.commit(); db.refresh(s)
        return s.id
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


def test_cached_digest_returns_200_for_any_tier(client):
    """A cached MeetingDigest row returns 200 with the legacy payload —
    no enqueue, no tier gate."""
    from database.database import SessionLocal
    from database.models import MeetingDigest

    org_id, org_slug, user_id = _seed_user("digest-cache", "digest_cache_user", "free")
    headers = _login(client, "digest_cache_user", org_slug)

    db = SessionLocal()
    try:
        d = MeetingDigest(
            organization_id=org_id,
            period="day",
            date="2026-05-29",
            content="Cached digest content for today.",
            meeting_count=3,
        )
        db.add(d); db.commit()
    finally:
        db.close()

    resp = client.get("/api/digests?period=day&date=2026-05-29", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cached"] is True
    assert body["content"] == "Cached digest content for today."


def test_cache_miss_pro_user_returns_202_with_job_id(client):
    """Cache-miss + pro tier enqueues + returns 202."""
    org_id, org_slug, user_id = _seed_user(
        "digest-miss-pro", "digest_miss_pro_user", "pro",
    )
    headers = _login(client, "digest_miss_pro_user", org_slug)
    _seed_completed_session(org_id, user_id)

    mock_job = MagicMock()
    mock_job.job_id = "arq-digest-1"
    mock_pool = AsyncMock()
    mock_pool.enqueue_job = AsyncMock(return_value=mock_job)

    with patch("services.job_runner.get_arq_pool", AsyncMock(return_value=mock_pool)):
        resp = client.get(
            "/api/digests?period=day&date=2026-05-28&force=true",
            headers=headers,
        )

    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["job_id"] == "arq-digest-1"
    assert body["status_url"] == "/api/jobs/arq-digest-1"
    assert body["period"] == "day"
    assert body["date"] == "2026-05-28"


def test_free_user_still_403s_before_enqueue(client):
    """Tier gate fires before enqueue — free user must see 403."""
    org_id, org_slug, user_id = _seed_user(
        "digest-miss-free", "digest_miss_free_user", "free",
    )
    headers = _login(client, "digest_miss_free_user", org_slug)
    _seed_completed_session(org_id, user_id)

    mock_pool = AsyncMock()
    with patch("services.job_runner.get_arq_pool", AsyncMock(return_value=mock_pool)):
        resp = client.get(
            "/api/digests?period=day&date=2026-05-27&force=true",
            headers=headers,
        )

    assert resp.status_code == 403, resp.text
    mock_pool.enqueue_job.assert_not_called()


@pytest.mark.asyncio
async def test_digest_worker_drift_check_skips_on_mismatch():
    """When the cached digest's `generation_job_id` no longer matches the
    running worker's ctx job_id, the worker bails."""
    from database.database import SessionLocal
    from database.models import MeetingDigest
    from workers.digest_workers import generate_digest_job

    org_id, _, _ = _seed_user("digest-drift", "digest_drift_user", "pro")

    db = SessionLocal()
    try:
        d = MeetingDigest(
            organization_id=org_id,
            period="day",
            date="2026-05-26",
            content="prev",
            meeting_count=0,
            generation_job_id="newer-job",
        )
        db.add(d); db.commit()
    finally:
        db.close()

    result = await generate_digest_job(
        {"job_id": "older-job"},
        org_id, "day", "2026-05-26",
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "drift"


@pytest.mark.asyncio
async def test_digest_worker_delegates_to_generate_digest():
    """The worker calls `_generate_digest` and returns its payload with
    status=completed."""
    from api.digests import DigestResponse
    from workers.digest_workers import generate_digest_job

    org_id, _, _ = _seed_user("digest-delegate", "digest_delegate_user", "pro")

    fake_response = DigestResponse(
        id=42, period="day", date="2026-05-25",
        content="Generated content.", meeting_count=2, cached=True,
    )

    async def fake_generate(db, oid, period, date_str, pid):
        return fake_response

    with patch("api.digests._generate_digest", side_effect=fake_generate):
        result = await generate_digest_job(
            {"job_id": "delegate-job"},
            org_id, "day", "2026-05-25",
        )

    assert result["status"] == "completed"
    assert result["content"] == "Generated content."
    assert result["meeting_count"] == 2
