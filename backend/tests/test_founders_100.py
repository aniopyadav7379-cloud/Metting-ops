"""Founding 100 mechanic tests (v3.21.0).

The locked design (Aaron, 2026-05-29 + cohort label v3.21.0):
  - Founding members pay the SAME $12/mo as everyone else. NOT a discount tier.
  - They get an `is_founding_member=True` + `founding_cohort` flag pair that
    controls early-access + ecosystem bundle eligibility (Project-Ops /
    Accounting-Ops).
  - Cap at 100 active founding members per cohort.
  - Flag is granted at Stripe annual-upfront completion (or, in v3.20.x and
    earlier, at signup) when FOUNDING_100_ACTIVE=true and the count of
    existing founders in the cohort is < FOUNDING_100_LIMIT.

These tests exercise the helper `_maybe_grant_founding_member` directly so we
can deterministically check the cap behavior without spinning up 100 signups.
"""

from __future__ import annotations

import uuid


def _models():
    from auth.models import User
    from database.database import SessionLocal
    return User, SessionLocal


def _make_user(*, is_founding_member: bool = False, founding_cohort=None):
    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        uname = "fnd" + uuid.uuid4().hex[:8]
        user = User(
            email=f"{uname}@example.com",
            username=uname,
            hashed_password="x",
            is_active=True,
            is_verified=True,
            tier="free",
            is_founding_member=is_founding_member,
            founding_cohort=founding_cohort,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def _founding_count(cohort: str = "meeting_ops_v1") -> int:
    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        return (
            db.query(User)
            .filter(User.is_founding_member.is_(True))
            .filter(User.founding_cohort == cohort)
            .count()
        )
    finally:
        db.close()


def _refresh(user):
    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user.id).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_founding_flag_set_when_active_and_under_limit(app, monkeypatch):
    from api.stripe_webhook import _maybe_grant_founding_member

    monkeypatch.setenv("FOUNDING_100_ACTIVE", "true")
    monkeypatch.setenv("FOUNDING_100_LIMIT", "100")
    user = _make_user()
    assert _refresh(user).is_founding_member is False

    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        live_user = db.query(User).filter(User.id == user.id).first()
        _maybe_grant_founding_member(db, live_user)
    finally:
        db.close()

    refreshed = _refresh(user)
    assert refreshed.is_founding_member is True
    assert refreshed.founding_cohort == "meeting_ops_v1"


def test_founding_flag_not_set_when_active_false(app, monkeypatch):
    from api.stripe_webhook import _maybe_grant_founding_member

    monkeypatch.setenv("FOUNDING_100_ACTIVE", "false")
    monkeypatch.delenv("FOUNDERS_100_ACTIVE", raising=False)
    user = _make_user()

    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        live_user = db.query(User).filter(User.id == user.id).first()
        _maybe_grant_founding_member(db, live_user)
    finally:
        db.close()

    assert _refresh(user).is_founding_member is False


def test_founding_flag_not_set_when_at_limit(app, monkeypatch):
    """If the existing count is >= FOUNDING_100_LIMIT for the cohort, new
    user does NOT get the flag. Aaron's locked behavior.
    """
    from api.stripe_webhook import _maybe_grant_founding_member

    monkeypatch.setenv("FOUNDING_100_ACTIVE", "true")
    limit_at = _founding_count("meeting_ops_v1")
    monkeypatch.setenv("FOUNDING_100_LIMIT", str(limit_at))
    user = _make_user()

    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        live_user = db.query(User).filter(User.id == user.id).first()
        _maybe_grant_founding_member(db, live_user)
    finally:
        db.close()

    assert _refresh(user).is_founding_member is False


def test_founding_grant_is_idempotent(app, monkeypatch):
    """Re-calling _maybe_grant_founding_member on an already-flagged user is a
    no-op (no extra commit, count unchanged).
    """
    from api.stripe_webhook import _maybe_grant_founding_member

    monkeypatch.setenv("FOUNDING_100_ACTIVE", "true")
    monkeypatch.setenv("FOUNDING_100_LIMIT", "1000")
    user = _make_user(is_founding_member=True, founding_cohort="meeting_ops_v1")
    before = _founding_count("meeting_ops_v1")

    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        live_user = db.query(User).filter(User.id == user.id).first()
        _maybe_grant_founding_member(db, live_user)
        _maybe_grant_founding_member(db, live_user)
    finally:
        db.close()

    after = _founding_count("meeting_ops_v1")
    assert before == after
    assert _refresh(user).is_founding_member is True


def test_founding_grant_blocked_when_cohort_admin_closed(app, monkeypatch):
    """POST /api/admin/founding/close flips an in-process closed flag.
    _maybe_grant_founding_member must respect it even when nominally
    below capacity.
    """
    from api.stripe_webhook import _maybe_grant_founding_member
    from api import founding as founding_module

    monkeypatch.setenv("FOUNDING_100_ACTIVE", "true")
    monkeypatch.setenv("FOUNDING_100_LIMIT", "1000")
    # Force the closed-set so we don't depend on Redis presence.
    founding_module._closed_cohorts_local.add("meeting_ops_v1")
    try:
        user = _make_user()
        User, SessionLocal = _models()
        db = SessionLocal()
        try:
            live_user = db.query(User).filter(User.id == user.id).first()
            granted = _maybe_grant_founding_member(db, live_user)
        finally:
            db.close()
        assert granted is False
        assert _refresh(user).is_founding_member is False
    finally:
        founding_module._closed_cohorts_local.discard("meeting_ops_v1")


def test_founding_status_endpoint_reports_capacity_and_open(app, client, monkeypatch):
    """/api/founding/status returns cohort + seats_taken + seats_total +
    is_open. Public (no auth)."""
    from api import founding as founding_module

    monkeypatch.setenv("FOUNDING_100_LIMIT", "100")
    # Bust the 60s cache to make the test deterministic.
    founding_module._status_cache.pop("meeting_ops_v1", None)
    founding_module._closed_cohorts_local.discard("meeting_ops_v1")

    r = client.get("/api/founding/status?cohort=meeting_ops_v1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cohort"] == "meeting_ops_v1"
    assert body["seats_total"] == 100
    assert isinstance(body["seats_taken"], int)
    assert body["is_open"] is True
