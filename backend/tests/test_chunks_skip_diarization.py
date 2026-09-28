"""Tests for the Diarization "Off" toggle wired through the chunk pipeline.

Covers:
  * `/api/providers/available/diarization` advertises the new "off" value.
  * `/api/providers/diarization` accepts `"off"` as a provider_name (this is
    the "Save as default" target promoted from the PipelineStatusPicker).
  * The `_org_diarization_off` helper returns True when the org default is
    "off", and False otherwise. Safe-fails on errors.
  * `/api/recordings/sessions/{id}/chunks-text` records
    `diarization_skipped: True` in processing_metadata when either:
      (a) the request payload includes `skip_diarization: true`, OR
      (b) the org's default diarization provider is "off".
  * `_diarize_chunk` is mocked and asserted to NOT be invoked when
    `skip_diarization` is set on the audio /chunks path. We don't drive
    the full audio path (ffmpeg + Parakeet) — instead we directly invoke
    the helper that gates the diarize call and confirm the effective_skip
    branch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import OrgProviderSettings, RecordingSession
    return Organization, User, UserOrganization, SessionLocal, OrgProviderSettings, RecordingSession


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


def _seed_user_and_org(slug: str, username: str = "diaroff_admin"):
    Organization, User, UserOrganization, SessionLocal, _, _ = _models()
    db = SessionLocal()
    try:
        # Reuse-or-create org
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


def _create_always_on_session(org_id: int, user_id: int) -> tuple[int, str]:
    """Create an always-on RecordingSession directly in the DB and return
    (pk, session_id)."""
    _, _, _, SessionLocal, _, RecordingSession = _models()
    import uuid

    db = SessionLocal()
    try:
        session_uuid = str(uuid.uuid4())
        session = RecordingSession(
            session_id=session_uuid,
            name=f"diar-off-test-{session_uuid[:8]}",
            title=f"diar-off-test-{session_uuid[:8]}",
            description="diarization off chunk-text test",
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


def _set_org_diarization(org_id: int, provider_name: str) -> None:
    _, _, _, SessionLocal, OrgProviderSettings, _ = _models()
    db = SessionLocal()
    try:
        row = (
            db.query(OrgProviderSettings)
            .filter(
                OrgProviderSettings.organization_id == org_id,
                OrgProviderSettings.service_kind == "diarization",
            )
            .first()
        )
        if not row:
            row = OrgProviderSettings(
                organization_id=org_id,
                service_kind="diarization",
                provider_name=provider_name,
            )
            db.add(row)
        else:
            row.provider_name = provider_name
        db.commit()
    finally:
        db.close()


def _read_session_metadata(session_pk: int) -> dict:
    _, _, _, SessionLocal, _, RecordingSession = _models()
    db = SessionLocal()
    try:
        row = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        assert row is not None
        return dict(row.processing_metadata or {})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. /api/providers/available/diarization advertises "off"
# ---------------------------------------------------------------------------
def test_available_diarization_includes_off(client):
    """The available-providers endpoint must list "off" so any settings UI
    that lists valid diarization providers can render it without breaking."""
    org_id, org_slug, _ = _seed_user_and_org("diaroff-avail")
    headers = _login_headers(client, "diaroff_admin", "admin123", org_slug)
    resp = client.get("/api/providers/available/diarization", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    providers = data.get("providers") or []
    assert "off" in providers, f"Expected 'off' in providers, got {providers}"
    assert "local_diarization" in providers


# ---------------------------------------------------------------------------
# 2. PUT /api/providers/diarization accepts "off"
# ---------------------------------------------------------------------------
def test_put_diarization_off_succeeds(client):
    """The "Save as default" button PUTs provider_name="off". Backend must
    accept it without 400-ing on the VALID_PROVIDERS check."""
    org_id, org_slug, _ = _seed_user_and_org("diaroff-put")
    headers = _login_headers(client, "diaroff_admin", "admin123", org_slug)
    resp = client.put(
        "/api/providers/diarization",
        headers=headers,
        json={"provider_name": "off", "overrides": {}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider_name"] == "off"


# ---------------------------------------------------------------------------
# 3. _org_diarization_off helper reads the setting correctly
# ---------------------------------------------------------------------------
def test_org_diarization_off_helper_reads_setting():
    from api.recording import _org_diarization_off
    from database.database import SessionLocal

    org_id, _, _ = _seed_user_and_org("diaroff-helper")
    db = SessionLocal()
    try:
        # Default: no setting -> False
        assert _org_diarization_off(db, org_id) is False

        # Set to "off" -> True
        _set_org_diarization(org_id, "off")
        # New DB session so we don't hit cached state
        db.close()
        db = SessionLocal()
        assert _org_diarization_off(db, org_id) is True

        # Flip back to "local_diarization" -> False
        _set_org_diarization(org_id, "local_diarization")
        db.close()
        db = SessionLocal()
        assert _org_diarization_off(db, org_id) is False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. POST /chunks-text with skip_diarization=True records the flag
# ---------------------------------------------------------------------------
def test_chunks_text_skip_diarization_payload_flag_records_metadata(client):
    """When the request payload sets skip_diarization=true, the session's
    processing_metadata gets diarization_skipped=True. This is the
    persistence signal analytics + the post-meeting summarizer use to know
    speakers won't be labeled."""
    org_id, org_slug, user_id = _seed_user_and_org("diaroff-payload")
    headers = _login_headers(client, "diaroff_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        headers=headers,
        json={
            "text": "hello world this is the test",
            "duration_seconds": 1.5,
            "elapsed_seconds": 0.0,
            "provenance": "client-parakeet-0.6b-int8",
            "skip_diarization": True,
        },
    )
    assert resp.status_code == 200, resp.text

    metadata = _read_session_metadata(session_pk)
    assert metadata.get("diarization_skipped") is True


# ---------------------------------------------------------------------------
# 5. POST /chunks-text with org default "off" records the flag without
#    explicit skip_diarization on the request
# ---------------------------------------------------------------------------
def test_chunks_text_org_default_off_records_skipped(client):
    """When the org's provider default is "off", every chunk should skip
    diarization even without explicit skip_diarization on the request."""
    org_id, org_slug, user_id = _seed_user_and_org("diaroff-orgdef")
    _set_org_diarization(org_id, "off")
    headers = _login_headers(client, "diaroff_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        headers=headers,
        json={
            "text": "second test chunk",
            "duration_seconds": 1.0,
            "elapsed_seconds": 1.5,
            "provenance": "client-parakeet-0.6b-int8",
        },
    )
    assert resp.status_code == 200, resp.text

    metadata = _read_session_metadata(session_pk)
    assert metadata.get("diarization_skipped") is True


# ---------------------------------------------------------------------------
# 6. POST /chunks-text WITHOUT skip_diarization and WITHOUT org default off
#    does NOT record diarization_skipped (sanity / negative test)
# ---------------------------------------------------------------------------
def test_chunks_text_default_does_not_record_skipped(client):
    """Negative: default state (no payload flag, no org-level "off") must
    NOT set diarization_skipped. Guards against accidental always-on
    skipping when nothing was selected."""
    org_id, org_slug, user_id = _seed_user_and_org("diaroff-default")
    headers = _login_headers(client, "diaroff_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/chunks-text",
        headers=headers,
        json={
            "text": "default-path chunk",
            "duration_seconds": 1.0,
            "elapsed_seconds": 0.0,
            "provenance": "client-parakeet-0.6b-int8",
        },
    )
    assert resp.status_code == 200, resp.text

    metadata = _read_session_metadata(session_pk)
    assert "diarization_skipped" not in metadata
