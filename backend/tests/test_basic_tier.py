"""v3.23.0 Basic-tier integration tests.

Basic tier ($7.99/mo, $79/yr) is the v3.23.0 architectural new arrival:

  Free  → browser-only, nothing on the server.
  Basic → server TEXT storage + sync + AI chat over corpus + cross-meeting
          search, but NO audio upload and NO server canonical_reprocess pass.
  Pro   → everything Basic has PLUS server audio (upload, canonical
          reprocess, diarization, speaker library, brigade graph writes).
  Suite → Pro + uc_suite_entitlement (cross-app benefit on Project-Ops +
          Contact-Ops via Brigade federation; documentation-only today).
  Enterprise → Suite + byok_models + retention_controls + hipaa.

The bright line we're pinning here: Basic gets text, Pro gets audio.

These tests exercise the inline gates on three load-bearing endpoints:
  - /api/uploads/start  (audio upload)              — Basic 403, Pro 200/202
  - /api/ai-chat/sessions/{id}/messages             — Basic 200 (text op)
  - /api/simple/recording-sessions/semantic-search  — Basic 200, Free 403
"""

from __future__ import annotations

import uuid as _uuid

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession
    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _login_headers(client, username, password, org_slug=None):
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _seed_user_and_org(slug, username, tier):
    Organization, User, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = (
            db.query(Organization).filter(Organization.slug == slug).first()
        )
        if not org:
            org = Organization(
                name=slug.replace("-", " ").title(),
                slug=slug,
                is_active=True,
            )
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
                is_superuser=False,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        user.tier = tier
        # billing-1: align the org plan with the seeded user tier (server
        # compute now also requires the ACTIVE org's plan to cover the
        # feature). Mirrors stripe_webhook._org_plan_for_tier.
        org.plan = (
            "enterprise" if tier == "enterprise"
            else "free" if (tier or "free") == "free"
            else "pro"
        )
        db.commit()
        mem = (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.organization_id == org.id,
            )
            .first()
        )
        if not mem:
            db.add(
                UserOrganization(
                    user_id=user.id,
                    organization_id=org.id,
                    role="admin",
                )
            )
            db.commit()
        return org.id, org.slug, user.id
    finally:
        db.close()


def _create_completed_session(org_id, user_id, transcript_text):
    """Create a RecordingSession with a stored transcript so the per-meeting
    AI chat endpoint has something to chat over. Returns (id, session_id)."""
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid,
            name=f"basic-tier-{sid[:8]}",
            title=f"basic-tier-{sid[:8]}",
            meeting_type="adhoc",
            mode="browser",
            status="completed",
            duration=120.0,
            user_id=user_id,
            organization_id=org_id,
            source_type="browser",
            transcript_simple=transcript_text,
            transcript=transcript_text,
            summary="Test summary for basic-tier chat coverage.",
            processing_metadata={"created_by": "test_basic_tier"},
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s.id, s.session_id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. TIER_FEATURES — the matrix pins the architectural rule
# ---------------------------------------------------------------------------


def test_basic_tier_flags_text_yes_audio_no():
    from auth.tier import TIER_FEATURES

    basic = TIER_FEATURES["basic"]
    # Text: yes.
    assert basic["server_text_storage"] is True
    assert basic["ai_chat_over_corpus"] is True
    assert basic["cross_meeting_search"] is True
    # Audio: no.
    assert basic["audio_upload"] is False
    assert basic["canonical_reprocess"] is False


def test_free_tier_has_neither_audio_nor_text_corpus():
    from auth.tier import TIER_FEATURES

    free = TIER_FEATURES["free"]
    # The whole point of Free: browser-only, no server text either.
    assert free["server_text_storage"] is False
    assert free["ai_chat_over_corpus"] is False
    assert free["cross_meeting_search"] is False
    assert free["audio_upload"] is False


# ---------------------------------------------------------------------------
# 2. Audio upload endpoint — Basic must be blocked
# ---------------------------------------------------------------------------


def test_basic_tier_blocked_from_uploads_transcribe(client, tmp_path, monkeypatch):
    """Basic ($7.99) gets text-only server processing. Audio upload is
    the Pro line — Basic gets 403 just like Free does."""
    from api import uploads

    monkeypatch.setattr(uploads, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", tmp_path / "uploads")

    org_id, org_slug, _user_id = _seed_user_and_org(
        "basic-upload", "basic_upload", "basic"
    )
    headers = _login_headers(client, "basic_upload", "admin123", org_slug)

    resp = client.post(
        "/api/uploads/start",
        headers=headers,
        json={
            "filename": "meeting.webm",
            "total_size": 1024,
            "content_type": "audio/webm",
            "action": "transcribe",
        },
    )
    assert resp.status_code == 403, resp.text


def test_pro_tier_allowed_through_uploads_transcribe(client, tmp_path, monkeypatch):
    """Sanity: Pro passes the audio_upload gate."""
    from api import uploads

    monkeypatch.setattr(uploads, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", tmp_path / "uploads")

    org_id, org_slug, _user_id = _seed_user_and_org(
        "basic-upload-pro", "basic_upload_pro", "pro"
    )
    headers = _login_headers(client, "basic_upload_pro", "admin123", org_slug)

    resp = client.post(
        "/api/uploads/start",
        headers=headers,
        json={
            "filename": "meeting.webm",
            "total_size": 1024,
            "content_type": "audio/webm",
            "action": "transcribe",
        },
    )
    assert resp.status_code in (200, 202), resp.text


# ---------------------------------------------------------------------------
# 3. Cross-meeting semantic search — Basic gets in, Free does not
# ---------------------------------------------------------------------------


def test_free_tier_blocked_from_cross_meeting_search(client):
    """Free has no server text storage, so cross-meeting search has nothing
    to search and is gated at the same layer as the corpus itself."""
    org_id, org_slug, _user_id = _seed_user_and_org(
        "basic-search-free", "basic_search_free", "free"
    )
    headers = _login_headers(client, "basic_search_free", "admin123", org_slug)

    resp = client.get(
        "/api/simple/recording-sessions/semantic-search",
        headers=headers,
        params={"q": "budget"},
    )
    assert resp.status_code == 403, resp.text
    assert "cross_meeting_search" in resp.text


def test_basic_tier_allowed_through_cross_meeting_search(client):
    """Basic users get to run cross-meeting search. The endpoint may 500
    in test if Qdrant is unavailable, but it must NOT 403 on tier."""
    org_id, org_slug, _user_id = _seed_user_and_org(
        "basic-search-basic", "basic_search_basic", "basic"
    )
    headers = _login_headers(client, "basic_search_basic", "admin123", org_slug)

    resp = client.get(
        "/api/simple/recording-sessions/semantic-search",
        headers=headers,
        params={"q": "budget"},
    )
    # The tier gate passed; the endpoint may bail at the Qdrant boundary
    # with a 500 in tests (no vector store), which is fine here — we
    # only care that Basic isn't 403'd.
    assert resp.status_code != 403, resp.text


# ---------------------------------------------------------------------------
# 4. Per-meeting AI chat over the corpus — Basic allowed
# ---------------------------------------------------------------------------


def test_basic_tier_allowed_through_per_meeting_ai_chat(client, monkeypatch):
    """Basic users can ask questions about their own meetings (text op).
    We stub _llm_call so the test doesn't need a running llama.cpp."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "basic-chat", "basic_chat", "basic"
    )
    headers = _login_headers(client, "basic_chat", "admin123", org_slug)
    _, session_id = _create_completed_session(
        org_id, user_id,
        "Speaker 1: We talked about Q4 budget. Speaker 2: Approved.",
    )

    # Stub the LLM resolution so the test doesn't need a real backend.
    from api import ai_chat as _ai_chat

    def _fake_llm_call(db, org_id, *, task, system_prompt, user_prompt,
                      max_tokens=500, temperature=0.7):
        return "Yes, Q4 budget was approved."

    monkeypatch.setattr(_ai_chat, "_llm_call", _fake_llm_call)

    resp = client.post(
        f"/api/ai-chat/sessions/{session_id}/messages",
        headers=headers,
        json={"message": "Was the Q4 budget approved?"},
    )
    # The tier gate must not 403 a Basic user. Downstream the endpoint
    # may still 5xx on environment issues (missing LLM provider config,
    # etc.) — that's a different concern; here we only check the gate.
    assert resp.status_code != 403, resp.text


def test_free_tier_blocked_from_per_meeting_ai_chat(client):
    """Free users get the browser-only experience. The server endpoint
    is closed to them via the ai_chat_over_corpus gate."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "basic-chat-free", "basic_chat_free", "free"
    )
    headers = _login_headers(client, "basic_chat_free", "admin123", org_slug)
    _, session_id = _create_completed_session(
        org_id, user_id,
        "Speaker 1: Test transcript.",
    )

    resp = client.post(
        f"/api/ai-chat/sessions/{session_id}/messages",
        headers=headers,
        json={"message": "What was discussed?"},
    )
    assert resp.status_code == 403, resp.text
    assert "ai_chat_over_corpus" in resp.text
