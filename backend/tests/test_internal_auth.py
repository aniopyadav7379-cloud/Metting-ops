"""Tests for the backend-internal service-token dual-auth dependency (task #96).

Covers:

  * Chunks endpoint with a valid ``X-Internal-Service-Token`` succeeds
    regardless of org context (no JWT, no X-MeetingOps-Org header).
  * Chunks endpoint with an INVALID internal token and no user auth is
    rejected (401).
  * Chunks endpoint with no token but valid user auth still works
    (backward compatibility with the browser always-on path).
  * Chunks endpoint with neither user auth nor internal token is rejected
    (401).
  * Token-leak attempt: invalid internal token + valid user auth →
    request still succeeds as the user; the audit log does NOT record
    the call as ``[internal]``.
  * Other endpoints (NOT chunks) reject internal-token-only requests:
    the token is scoped to the chunks loopback path.
  * Empty ``INTERNAL_SERVICE_TOKEN`` env var fail-closes: even if the
    request presents the right "previous" token value, an empty
    configured value can never match.

Token comparison uses ``secrets.compare_digest`` (constant-time) inside
``auth/internal.py`` so a timing oracle on the token can't be built.
"""
from __future__ import annotations

import os
import uuid
from typing import Tuple
from unittest.mock import patch

import pytest

from auth.utils import get_password_hash


VALID_TOKEN = "test-internal-token-aaaa-bbbb-cccc-dddd-eeee-ffff-0011-2233"
WRONG_TOKEN = "test-internal-token-not-the-right-value-0123456789abcdef"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession

    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _seed_user_and_org(slug: str, username: str = "internal_auth_admin") -> Tuple[int, str, int]:
    Organization, User, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True, plan="enterprise")  # billing-1: paid workspace matches the enterprise user
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
                tier="enterprise",  # paid tier: server-processing endpoints are gated (v3.0.0)
                is_superuser=False,
            )
            db.add(user)
            db.commit()
            db.refresh(user)

        mem = (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.organization_id == org.id,
            )
            .first()
        )
        if not mem:
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin"))
            db.commit()
        return org.id, org.slug, user.id
    finally:
        db.close()


def _create_always_on_session(org_id: int, user_id: int) -> Tuple[int, str]:
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        session_uuid = str(uuid.uuid4())
        session = RecordingSession(
            session_id=session_uuid,
            name=f"internal-auth-test-{session_uuid[:8]}",
            title=f"internal-auth-test-{session_uuid[:8]}",
            description="internal token chunk-text test",
            meeting_type="always_on",
            mode="always_on",
            status="recording",
            duration=0.0,
            user_id=user_id,
            organization_id=org_id,
            source_type="browser_always_on",
            processing_metadata={"created_by": "test"},
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session.id, session.session_id
    finally:
        db.close()


def _login_headers(client, username: str, password: str, org_slug: str | None = None) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert response.status_code == 200, f"Login failed: {response.text}"
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _chunk_payload(provenance: str = "test") -> dict:
    return {
        "text": "internal token test chunk",
        "duration_seconds": 1.0,
        "elapsed_seconds": 0.0,
        "provenance": provenance,
    }


@pytest.fixture()
def with_token():
    """Patch the env var that ``auth.internal._configured_token`` reads.

    Patching ``os.environ`` is enough because the helper re-reads on
    every call (no module-level caching of the value).
    """
    with patch.dict(os.environ, {"INTERNAL_SERVICE_TOKEN": VALID_TOKEN}):
        yield


@pytest.fixture()
def without_token():
    """Env-var explicitly cleared so the fail-closed branch is exercised."""
    with patch.dict(os.environ, {"INTERNAL_SERVICE_TOKEN": ""}):
        yield


# ---------------------------------------------------------------------------
# 1. Valid internal token succeeds (no user auth, no org header)
# ---------------------------------------------------------------------------
def test_chunks_text_with_valid_internal_token_succeeds(client, with_token):
    org_id, _, user_id = _seed_user_and_org("internal-valid-token")
    _, session_id = _create_always_on_session(org_id, user_id)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        headers={"X-Internal-Service-Token": VALID_TOKEN},
        json=_chunk_payload("internal-test-valid"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["segment"]["provenance"] == "internal-test-valid"


# ---------------------------------------------------------------------------
# 2. Invalid internal token, no user auth → 401
# ---------------------------------------------------------------------------
def test_chunks_text_with_invalid_token_no_user_rejected(client, with_token):
    org_id, _, user_id = _seed_user_and_org("internal-bad-token")
    _, session_id = _create_always_on_session(org_id, user_id)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        headers={"X-Internal-Service-Token": WRONG_TOKEN},
        json=_chunk_payload(),
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 3. No internal token, valid user auth → success (backward compat)
# ---------------------------------------------------------------------------
def test_chunks_text_user_auth_only_still_works(client, with_token):
    org_id, org_slug, user_id = _seed_user_and_org(
        "internal-user-still-works", username="internal_user_only"
    )
    _, session_id = _create_always_on_session(org_id, user_id)
    headers = _login_headers(client, "internal_user_only", "admin123", org_slug)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        headers=headers,
        json=_chunk_payload("user-only"),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 4. Neither token nor user auth → 401
# ---------------------------------------------------------------------------
def test_chunks_text_neither_token_nor_user_rejected(client, with_token):
    org_id, _, user_id = _seed_user_and_org("internal-neither")
    _, session_id = _create_always_on_session(org_id, user_id)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        json=_chunk_payload(),
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 5. Invalid token + valid user auth → request succeeds as the user
#    (no [internal] log emission)
# ---------------------------------------------------------------------------
def test_invalid_token_with_valid_user_falls_through_to_user(client, with_token, caplog):
    org_id, org_slug, user_id = _seed_user_and_org(
        "internal-fallthrough", username="internal_fallthrough"
    )
    _, session_id = _create_always_on_session(org_id, user_id)
    headers = _login_headers(client, "internal_fallthrough", "admin123", org_slug)
    headers["X-Internal-Service-Token"] = WRONG_TOKEN

    with caplog.at_level("INFO", logger="api.recording"):
        resp = client.post(
            f"/api/recordings/sessions/{session_id}/chunks-text",
            headers=headers,
            json=_chunk_payload("fallthrough"),
        )
    assert resp.status_code == 200, resp.text
    # Internal-marker line MUST NOT appear: the bad token was ignored,
    # the user auth carried the day.
    assert not any("[internal]" in rec.getMessage() for rec in caplog.records), (
        "Invalid internal token should not flag the request as internal."
    )


# ---------------------------------------------------------------------------
# 6. Token does NOT unlock other endpoints — scoped to chunks only.
# ---------------------------------------------------------------------------
def test_internal_token_does_not_unlock_other_endpoints(client, with_token):
    # /api/auth/me is the canonical "needs a real user" endpoint and
    # mounts no internal-token dependency. Even with a valid internal
    # token, the request must fail because there's no user identity to
    # describe in the response.
    resp = client.get(
        "/api/auth/me",
        headers={"X-Internal-Service-Token": VALID_TOKEN},
    )
    # Either 401 (no auth) or 403 (rejected without org membership) —
    # what matters is that it is NOT 200.
    assert resp.status_code != 200, (
        f"Internal token unexpectedly authenticated /api/auth/me: {resp.status_code} {resp.text}"
    )


# ---------------------------------------------------------------------------
# 7. Empty INTERNAL_SERVICE_TOKEN env-var fail-closes
# ---------------------------------------------------------------------------
def test_empty_env_var_fail_closes_against_token_attempt(client, without_token):
    org_id, _, user_id = _seed_user_and_org("internal-empty-env")
    _, session_id = _create_always_on_session(org_id, user_id)

    # Present *some* token; with the configured side empty, ANY presented
    # value must lose constant-time compare against the empty string.
    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        headers={"X-Internal-Service-Token": VALID_TOKEN},
        json=_chunk_payload(),
    )
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 8. Internal call with a session from a foreign org still writes to the
#    correct org (the session's own org), NOT to whatever org the
#    request might try to claim via header. Defence against a tampered
#    X-MeetingOps-Org header on an internal call.
# ---------------------------------------------------------------------------
def test_internal_token_uses_session_org_not_header_org(client, with_token):
    from auth.models import Organization
    from database.database import SessionLocal
    from database.models import RecordingSession

    # Two orgs. Session lives in alpha; request claims bravo via header.
    alpha_id, alpha_slug, alpha_user = _seed_user_and_org(
        "internal-alpha-org", username="alpha_admin"
    )
    bravo_id, bravo_slug, _ = _seed_user_and_org(
        "internal-bravo-org", username="bravo_admin"
    )
    session_pk, session_id = _create_always_on_session(alpha_id, alpha_user)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        headers={
            "X-Internal-Service-Token": VALID_TOKEN,
            "X-MeetingOps-Org": bravo_slug,  # red-herring header
        },
        json=_chunk_payload("org-confused-deputy"),
    )
    assert resp.status_code == 200, resp.text

    db = SessionLocal()
    try:
        row = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        assert row is not None
        # Org never changed — the bravo header must be ignored on internal
        # calls and the session's own org is the only writable target.
        assert row.organization_id == alpha_id, (
            f"Internal call honoured X-MeetingOps-Org={bravo_slug} and wrote "
            f"to org {row.organization_id} instead of the session's own org {alpha_id}."
        )
    finally:
        db.close()
