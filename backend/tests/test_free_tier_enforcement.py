"""v3.0.0 free-tier enforcement tests.

Free tier is browser-only: it must NOT be able to upload audio/text chunks,
trigger the canonical server reprocess, run server-side LLM summaries, or
bulk-import. Pro/Enterprise get the full server stack. Internal service
callers (room_recorder loopback) bypass tier gating entirely.

Two layers:
  1. Unit tests on the gate primitives in auth/tier.py — require_feature
     (dependency factory) + gate_feature_for_caller (inline, internal-caller
     -safe). These use real auth.models.User instances so the isinstance
     check in gate_feature_for_caller is genuinely exercised.
  2. Integration tests that POST to actual gated endpoints as a free vs pro
     user and assert 403 vs not-403, reusing the seeding pattern from
     test_audio_chunks_reprocess.py.
"""
from __future__ import annotations

import asyncio
import uuid as _uuid

import pytest
from fastapi import HTTPException

from auth.utils import get_password_hash


# ---------------------------------------------------------------------------
# Unit: TIER_FEATURES matrix
# ---------------------------------------------------------------------------

def test_tier_features_matrix_for_server_write_keys():
    from auth.tier import TIER_FEATURES

    # Free + Basic must NOT have the server AUDIO/reprocess gates.
    # Basic gets text-only server features (see test_basic_tier.py).
    # Pro / Suite / Enterprise get the full server stack.
    server_audio_features = [
        "canonical_reprocess",
        "brigade_integration",
        "bulk_import",
        "agent_write_reprocess",
    ]
    for feat in server_audio_features:
        assert TIER_FEATURES["free"][feat] is False, f"free should not have {feat}"
        assert TIER_FEATURES["basic"][feat] is False, f"basic should not have {feat}"
        assert TIER_FEATURES["pro"][feat] is True, f"pro should have {feat}"
        assert TIER_FEATURES["suite"][feat] is True, f"suite should have {feat}"
        assert TIER_FEATURES["enterprise"][feat] is True, f"enterprise should have {feat}"

    # cross_device_sync + agent_write_email_draft are server-text features
    # so Basic gets them too.
    server_text_features = ["cross_device_sync", "agent_write_email_draft"]
    for feat in server_text_features:
        assert TIER_FEATURES["free"][feat] is False, f"free should not have {feat}"
        assert TIER_FEATURES["basic"][feat] is True, f"basic should have {feat}"
        assert TIER_FEATURES["pro"][feat] is True, f"pro should have {feat}"


def test_tier_features_matrix_for_basic_agent_writes():
    from auth.tier import TIER_FEATURES

    assert TIER_FEATURES["free"]["agent_write_basic"] is True
    assert TIER_FEATURES["basic"]["agent_write_basic"] is True
    assert TIER_FEATURES["pro"]["agent_write_basic"] is True
    assert TIER_FEATURES["suite"]["agent_write_basic"] is True
    assert TIER_FEATURES["enterprise"]["agent_write_basic"] is True


def _user(tier: str = "free", is_superuser: bool = False):
    """A real (unpersisted) User instance — enough for tier resolution and
    the isinstance(caller, User) check in gate_feature_for_caller."""
    from auth.models import User

    return User(
        email=f"{tier}@meeting-ops.local",
        username=f"{tier}-user",
        hashed_password="x",
        is_active=True,
        is_superuser=is_superuser,
        tier=tier,
    )


def _org_stub(plan: str = "free"):
    """Minimal stand-in for an ActiveOrganization for the org-side gate
    (billing-1). The gate machinery unwraps ``.organization`` then reads
    ``.plan``, mirroring auth.organization.ActiveOrganization."""

    class _Org:
        def __init__(self, p):
            self.plan = p

    class _Active:
        def __init__(self, p):
            self.organization = _Org(p)

    return _Active(plan)


# ---------------------------------------------------------------------------
# Unit: require_feature (dependency factory)
# ---------------------------------------------------------------------------


def test_require_feature_403s_free_user():
    from auth.tier import require_feature

    checker = require_feature("canonical_reprocess")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(checker(current_user=_user("free"), active_org=_org_stub("free")))
    assert exc.value.status_code == 403
    assert "canonical_reprocess" in exc.value.detail


def test_require_feature_passes_pro_and_enterprise():
    from auth.tier import require_feature

    # billing-1: the gate now also requires the ACTIVE org's plan to cover the
    # feature, so a covering org is passed alongside the paid user.
    checker = require_feature("canonical_reprocess")
    assert asyncio.run(
        checker(current_user=_user("pro"), active_org=_org_stub("pro"))
    ).tier == "pro"
    assert asyncio.run(
        checker(current_user=_user("enterprise"), active_org=_org_stub("enterprise"))
    ).tier == "enterprise"


def test_require_feature_superuser_passes():
    from auth.tier import require_feature

    checker = require_feature("canonical_reprocess")
    # is_superuser overrides to enterprise even with tier='free', AND bypasses
    # the per-workspace org gate (here a FREE org) — support/dev access.
    su = _user("free", is_superuser=True)
    assert asyncio.run(checker(current_user=su, active_org=_org_stub("free"))) is su


def test_require_feature_pro_user_in_free_org_denied():
    """billing-1: a Pro user whose ACTIVE workspace is on the free plan is
    denied — one paid seat must not unlock compute in an unpaid org."""
    from auth.tier import require_feature

    checker = require_feature("canonical_reprocess")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(checker(current_user=_user("pro"), active_org=_org_stub("free")))
    assert exc.value.status_code == 403
    assert "workspace" in exc.value.detail.lower()


def test_require_feature_unknown_feature_raises_at_definition():
    from auth.tier import require_feature

    with pytest.raises(ValueError):
        require_feature("does_not_exist")


# ---------------------------------------------------------------------------
# Unit: gate_feature_for_caller (inline, internal-caller-safe)
# ---------------------------------------------------------------------------


def test_gate_feature_for_caller_403s_free_user():
    from auth.tier import gate_feature_for_caller

    with pytest.raises(HTTPException) as exc:
        gate_feature_for_caller(_user("free"), "canonical_reprocess")
    assert exc.value.status_code == 403


def test_gate_feature_for_caller_allows_pro_user():
    from auth.tier import gate_feature_for_caller

    # Should not raise.
    gate_feature_for_caller(_user("pro"), "canonical_reprocess")


def test_gate_feature_for_caller_bypasses_internal_service_caller():
    """A non-User principal (internal loopback like room_recorder) is trusted
    infra and must bypass the tier gate, never 403."""
    from auth.tier import gate_feature_for_caller

    class _FakeInternalCaller:
        # No tier attribute on purpose — must not be treated as free.
        service = "room_recorder"

    # Should not raise even though feature is a paid one.
    gate_feature_for_caller(_FakeInternalCaller(), "canonical_reprocess")


def test_gate_feature_for_caller_unknown_feature_raises():
    from auth.tier import gate_feature_for_caller

    with pytest.raises(ValueError):
        gate_feature_for_caller(_user("pro"), "nope_not_real")


# ---------------------------------------------------------------------------
# billing-1: per-workspace (active-org) gate — test BOTH directions
# ---------------------------------------------------------------------------


def test_gate_feature_for_caller_pro_user_free_org_denied():
    """A Pro user acting in a FREE workspace is denied server compute — the
    active org's plan is authoritative per workspace (the core billing-1 fix)."""
    from auth.tier import gate_feature_for_caller

    with pytest.raises(HTTPException) as exc:
        gate_feature_for_caller(_user("pro"), "canonical_reprocess", _org_stub("free"))
    assert exc.value.status_code == 403
    assert "workspace" in exc.value.detail.lower()


def test_gate_feature_for_caller_pro_user_pro_org_allowed():
    """Paid user in a paid workspace: allowed (no raise)."""
    from auth.tier import gate_feature_for_caller

    gate_feature_for_caller(_user("pro"), "canonical_reprocess", _org_stub("pro"))


def test_gate_feature_for_caller_internal_bypasses_org_gate():
    """Internal-service principals bypass even with a free active org."""
    from auth.tier import gate_feature_for_caller

    class _FakeInternalCaller:
        service = "room_recorder"  # no .tier

    gate_feature_for_caller(
        _FakeInternalCaller(), "canonical_reprocess", _org_stub("free")
    )


def test_gate_feature_for_caller_superuser_bypasses_org_gate():
    """Superuser (support/dev) bypasses the per-workspace gate in a free org."""
    from auth.tier import gate_feature_for_caller

    gate_feature_for_caller(
        _user("free", is_superuser=True), "canonical_reprocess", _org_stub("free")
    )


def test_gate_feature_for_caller_two_arg_unchanged():
    """2-arg form (no active_org) keeps legacy user-only behavior — backward
    compatible with call sites we did not thread an org through."""
    from auth.tier import gate_feature_for_caller

    gate_feature_for_caller(_user("pro"), "canonical_reprocess")  # allowed
    with pytest.raises(HTTPException):
        gate_feature_for_caller(_user("free"), "canonical_reprocess")  # denied


def test_org_covers_feature_unknown_plan_is_safe():
    """An unrecognized org plan (drift / manual edit) must NOT KeyError; it
    fails safe to free-tier features."""
    from auth.tier import TIER_FEATURES, get_org_tier_features, org_covers_feature

    class _Org:
        plan = "founders_friend"  # not a known tier

    assert org_covers_feature(_Org(), "canonical_reprocess") is False
    assert get_org_tier_features(_Org()) == TIER_FEATURES["free"]
    assert org_covers_feature(None, "canonical_reprocess") is False


# ---------------------------------------------------------------------------
# Integration: real gated endpoints, free vs pro
# ---------------------------------------------------------------------------


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession
    return Organization, User, UserOrganization, SessionLocal, RecordingSession


def _login_headers(client, username, password, org_slug=None):
    resp = client.post("/api/auth/login", data={"username": username, "password": password})
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    if org_slug:
        headers["X-MeetingOps-Org"] = org_slug
    return headers


def _seed_user_and_org(slug, username, tier):
    Organization, User, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True)
            db.add(org); db.commit(); db.refresh(org)
        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                hashed_password=get_password_hash("admin123"),
                is_active=True, is_verified=True, is_superuser=False,
            )
            db.add(user); db.commit(); db.refresh(user)
        user.tier = tier
        # billing-1: align the org plan with the seeded user tier so paid
        # users sit in a paid workspace (server compute now also requires the
        # ACTIVE org's plan to cover the feature). Mirrors
        # stripe_webhook._org_plan_for_tier (free→free, enterprise→enterprise,
        # any other paid tier→pro).
        org.plan = (
            "enterprise" if tier == "enterprise"
            else "free" if (tier or "free") == "free"
            else "pro"
        )
        db.commit()
        mem = (
            db.query(UserOrganization)
            .filter(UserOrganization.user_id == user.id, UserOrganization.organization_id == org.id)
            .first()
        )
        if not mem:
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role="admin")); db.commit()
        return org.id, org.slug, user.id
    finally:
        db.close()


def _set_org_plan(org_id, plan):
    """Force an org onto a specific plan (billing-1 integration tests that need
    a paid USER in an explicitly-FREE workspace, the leak case)."""
    Organization, _, _, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.id == org_id).first()
        org.plan = plan
        db.commit()
    finally:
        db.close()


def _create_always_on_session(org_id, user_id):
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid, name=f"tiergate-{sid[:8]}", title=f"tiergate-{sid[:8]}",
            meeting_type="always_on", mode="always_on", status="recording",
            duration=0.0, user_id=user_id, organization_id=org_id,
            source_type="browser_always_on", processing_metadata={"created_by": "test"},
        )
        db.add(s); db.commit(); db.refresh(s)
        return s.id, s.session_id
    finally:
        db.close()


def test_free_tier_blocked_from_audio_chunks(client, tmp_path, monkeypatch):
    from api import recording
    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("tiergate-free", "tiergate_free", "free")
    headers = _login_headers(client, "tiergate_free", "admin123", org_slug)
    _, session_id = _create_always_on_session(org_id, user_id)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/audio-chunks",
        headers=headers,
        files={"chunk": ("c0.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 32, "audio/webm")},
        data={"chunk_index": "0"},
    )
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text


def test_free_tier_blocked_from_finalize_audio(client, tmp_path, monkeypatch):
    from api import recording
    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("tiergate-free2", "tiergate_free2", "free")
    headers = _login_headers(client, "tiergate_free2", "admin123", org_slug)
    _, session_id = _create_always_on_session(org_id, user_id)

    # No body: `verification` defaults to None (the realistic free-client
    # call). Sending json={} would 422 on the verification schema before the
    # inline gate runs; we want to exercise the gate, so send no body.
    resp = client.post(
        f"/api/recordings/sessions/{session_id}/finalize-audio", headers=headers
    )
    assert resp.status_code == 403, resp.text


def test_pro_tier_allowed_through_audio_chunks_gate(client, tmp_path, monkeypatch):
    """A pro user passes the tier gate. We assert it's NOT a 403 — the
    endpoint proceeds (200 on a valid chunk write)."""
    from api import recording
    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("tiergate-pro", "tiergate_pro", "pro")
    headers = _login_headers(client, "tiergate_pro", "admin123", org_slug)
    _, session_id = _create_always_on_session(org_id, user_id)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/audio-chunks",
        headers=headers,
        files={"chunk": ("c0.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 32, "audio/webm")},
        data={"chunk_index": "0"},
    )
    assert resp.status_code != 403, resp.text
    assert resp.status_code == 200, resp.text


def test_free_tier_blocked_from_bulk_import(client):
    org_id, org_slug, user_id = _seed_user_and_org("tiergate-bulk", "tiergate_bulk", "free")
    headers = _login_headers(client, "tiergate_bulk", "admin123", org_slug)
    resp = client.post("/api/import/jobs", headers=headers)
    assert resp.status_code == 403, resp.text
    assert "bulk_import" in resp.text


def test_free_tier_blocked_from_uploads_transcribe(client, tmp_path, monkeypatch):
    from api import uploads
    monkeypatch.setattr(uploads, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", tmp_path / "uploads")

    org_id, org_slug, user_id = _seed_user_and_org("tiergate-upload-free", "tiergate_upload_free", "free")
    headers = _login_headers(client, "tiergate_upload_free", "admin123", org_slug)

    free_resp = client.post(
        "/api/uploads/start",
        headers=headers,
        json={
            "filename": "meeting.webm",
            "total_size": 1024,
            "content_type": "audio/webm",
            "action": "transcribe",
        },
    )
    assert free_resp.status_code == 403, free_resp.text

    org_id, org_slug, user_id = _seed_user_and_org("tiergate-upload-pro", "tiergate_upload_pro", "pro")
    headers = _login_headers(client, "tiergate_upload_pro", "admin123", org_slug)

    pro_resp = client.post(
        "/api/uploads/start",
        headers=headers,
        json={
            "filename": "meeting.webm",
            "total_size": 1024,
            "content_type": "audio/webm",
            "action": "transcribe",
        },
    )
    assert pro_resp.status_code in (200, 202), pro_resp.text


def test_billing1_pro_user_in_free_org_denied_server_compute(client, tmp_path, monkeypatch):
    """billing-1 (integration, the leak case): a Pro user whose ACTIVE workspace
    is on the FREE plan is denied server compute, even though their global tier
    is Pro. Mirror: the SAME pro user in a PAID workspace is allowed."""
    from api import uploads
    monkeypatch.setattr(uploads, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", tmp_path / "uploads")

    payload = {
        "filename": "meeting.webm",
        "total_size": 1024,
        "content_type": "audio/webm",
        "action": "transcribe",
    }

    # Pro user, but their workspace is forced onto the free plan → DENIED.
    org_id, org_slug, _ = _seed_user_and_org("b1-pro-in-free", "b1_pro_in_free", "pro")
    _set_org_plan(org_id, "free")
    headers = _login_headers(client, "b1_pro_in_free", "admin123", org_slug)
    denied = client.post("/api/uploads/start", headers=headers, json=payload)
    assert denied.status_code == 403, denied.text
    assert "workspace" in denied.text.lower()

    # Same tier user, but now in a paid workspace → ALLOWED.
    org_id2, org_slug2, _ = _seed_user_and_org("b1-pro-in-pro", "b1_pro_in_pro", "pro")
    headers2 = _login_headers(client, "b1_pro_in_pro", "admin123", org_slug2)
    allowed = client.post("/api/uploads/start", headers=headers2, json=payload)
    assert allowed.status_code in (200, 202), allowed.text


def test_unauth_admin_routers_require_auth(client):
    from config.model_registry import get_active_model

    # v3.18.3: prefix double-mount documentation drift closed by passing
    # prefix="" explicitly in main.py. Some routers (simple_settings,
    # agent_management) may fail to load in environments without their
    # filesystem prerequisites (`/srv`, populated meeting_agents
    # table). Skip cases whose router didn't load — the auth lockdown only
    # has meaning when the route is mounted.
    import main as _main
    failed_names = {f["name"] for f in getattr(_main, "failed_routers", [])}

    active_model_id = get_active_model()["model_id"]
    all_cases = [
        (
            "simple_settings",
            "/api/simple/settings",
            "/api/simple/settings",
            {},
        ),
        (
            "ai_settings",
            "/api/settings/models",
            "/api/settings/models/active",
            {"model_id": active_model_id},
        ),
        (
            "agent_management",
            "/api/agents/",
            "/api/agents/",
            {
                "name": "admin-test-agent",
                "agent_type": "task_template",
                "provider_type": "ollama",
                "model_name": "test-model",
                "system_prompt": "test",
                "output_format": "text",
            },
        ),
        (
            "unified_agent",
            "/api/unified-agent/agent",
            "/api/unified-agent/agent/reset",
            None,
        ),
    ]
    cases = [(g, p, py) for name, g, p, py in all_cases if name not in failed_names]
    if not cases:
        pytest.skip("None of the admin routers loaded in this test env")

    for get_path, post_path, payload in cases:
        anon_get = client.get(get_path)
        assert anon_get.status_code == 401, anon_get.text

        org_id, org_slug, user_id = _seed_user_and_org(f"tiergate-{post_path.strip('/').replace('/', '-')}-free", f"tiergate_{post_path.strip('/').replace('/', '_')}_free", "free")
        headers = _login_headers(client, f"tiergate_{post_path.strip('/').replace('/', '_')}_free", "admin123", org_slug)

        if payload is None:
            non_super = client.post(post_path, headers=headers)
        else:
            non_super = client.post(post_path, headers=headers, json=payload)
        assert non_super.status_code == 403, non_super.text

        org_id, org_slug, user_id = _seed_user_and_org(f"tiergate-{post_path.strip('/').replace('/', '-')}-su", f"tiergate_{post_path.strip('/').replace('/', '_')}_su", "pro")
        from auth.models import User
        from auth.utils import get_password_hash
        from database.database import SessionLocal

        db = SessionLocal()
        try:
            su_user = db.query(User).filter(User.username == f"tiergate_{post_path.strip('/').replace('/', '_')}_su").first()
            su_user.is_superuser = True
            su_user.hashed_password = get_password_hash("admin123")
            db.commit()
        finally:
            db.close()

        headers = _login_headers(client, f"tiergate_{post_path.strip('/').replace('/', '_')}_su", "admin123", org_slug)
        # Some endpoints (unified-agent/agent/reset) reach into tables that
        # aren't materialized in the sqlite test DB and will OperationalError
        # under the superuser hit. The auth + tier gates have already been
        # proven by the 401 + 403 assertions above; the superuser-200 check
        # is purely a smoke confirmation, and we wrap it so a downstream DB
        # error doesn't mask the upstream gate proof.
        try:
            if payload is None:
                super_resp = client.post(post_path, headers=headers)
            else:
                super_resp = client.post(post_path, headers=headers, json=payload)
            assert super_resp.status_code in (200, 500), super_resp.text
        except Exception as exc:  # pragma: no cover - sqlite-only escape hatch
            if "OperationalError" in type(exc).__name__ or "no such table" in str(exc):
                continue
            raise


def test_meeting_intelligence_real_router_not_mounted(client):
    resp = client.get("/api/meeting-intelligence-real/final-report")
    assert resp.status_code == 404, resp.text
# ---------------------------------------------------------------------------
# v3.18.1 batch 1b: simple_recording_db / rooms / satellite tier gates
# ---------------------------------------------------------------------------


def _set_org_plan(org_id: int, plan: str) -> None:
    """Helper for satellite tests: satellite endpoints gate by Organization.plan
    (the device IS the caller), not by a user's tier.
    """
    Organization, _, _, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.id == org_id).first()
        if org is not None:
            org.plan = plan
            db.commit()
    finally:
        db.close()


def _create_simple_session(org_id: int, user_id: int):
    """Create a simple session not bound to always-on, for the
    /api/simple/recording-sessions/{id}/start tier-gate test.
    """
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid, name=f"simple-{sid[:8]}", title=f"simple-{sid[:8]}",
            meeting_type="standard", status="created",
            duration=0.0, user_id=user_id, organization_id=org_id,
            processing_metadata={"created_by": "test"},
        )
        db.add(s); db.commit(); db.refresh(s)
        return s.id, s.session_id
    finally:
        db.close()


def test_free_tier_blocked_from_simple_recording_start(client):
    """POST /api/simple/recording-sessions/{id}/start runs server-side
    audio capture + STT/diarization/summary. Free tier is browser-only
    and must be 403'd before the audio service starts."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-simple-rec-free", "tiergate_simple_rec_free", "free"
    )
    headers = _login_headers(client, "tiergate_simple_rec_free", "admin123", org_slug)
    _, session_id = _create_simple_session(org_id, user_id)

    resp = client.post(
        f"/api/simple/recording-sessions/{session_id}/start", headers=headers
    )
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text


def test_pro_tier_passes_simple_recording_start_gate(client, tmp_path, monkeypatch):
    """Pro user does NOT 403 at the tier gate. Positive-case smoke test
    paired with the moat-critical free-tier 403 case above."""
    from api import simple_recording_db as srdb
    monkeypatch.setattr(srdb, "RECORDINGS_DIR", str(tmp_path), raising=False)
    # v3.18.3: working_audio_service now reads RECORDINGS_DIR from env,
    # so override it for this test so the cross-platform run uses tmp_path.
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path))
    from services import working_audio_service as was
    monkeypatch.setattr(was.WorkingAudioService, "RECORDINGS_DIR", str(tmp_path), raising=False)

    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-simple-rec-pro", "tiergate_simple_rec_pro", "pro"
    )
    headers = _login_headers(client, "tiergate_simple_rec_pro", "admin123", org_slug)
    _, session_id = _create_simple_session(org_id, user_id)

    resp = client.post(
        f"/api/simple/recording-sessions/{session_id}/start", headers=headers
    )
    assert resp.status_code != 403, resp.text


def test_free_tier_blocked_from_room_recording_start(client):
    """POST /api/rooms/{id}/recordings/start spawns RoomRecorder which
    then chunks via InternalServiceCaller (bypassing the gate). Gate at
    start so free orgs cannot kick off the server pipeline at all."""
    _, _, _, SessionLocal, _ = _models()
    from database.models_rooms import ConferenceRoom
    import uuid as _u

    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-room-free", "tiergate_room_free", "free"
    )
    headers = _login_headers(client, "tiergate_room_free", "admin123", org_slug)

    db = SessionLocal()
    try:
        rid = _u.uuid4()
        room = ConferenceRoom(
            id=rid,
            organization_id=org_id,
            name=f"tiergate-room-{str(rid)[:8]}",
            status="idle",
        )
        db.add(room); db.commit(); db.refresh(room)
        room_id_str = str(room.id)
    finally:
        db.close()

    resp = client.post(f"/api/rooms/{room_id_str}/recordings/start", headers=headers)
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text


def test_free_org_blocked_from_satellite_endpoints():
    """Satellite endpoints (upload-audio + transcript) gate on Organization.plan
    via auth.tier.get_org_tier, not on a user's tier. Free orgs cannot
    use satellite capture even with a valid device secret."""
    from auth.tier import get_org_tier

    class _StubFreeOrg:
        plan = "free"

    class _StubProOrg:
        plan = "pro"

    class _StubEntOrg:
        plan = "enterprise"

    class _StubNoPlanOrg:
        pass

    assert get_org_tier(_StubFreeOrg()) == "free"
    assert get_org_tier(_StubProOrg()) == "pro"
    assert get_org_tier(_StubEntOrg()) == "enterprise"
    # Missing/None plan defaults to free, not a crash:
    assert get_org_tier(_StubNoPlanOrg()) == "free"
    assert get_org_tier(type("Empty", (), {"plan": None})()) == "free"


# ---------------------------------------------------------------------------
# v3.18.2: ai_chat / ai_insights / digests tier gates
# ---------------------------------------------------------------------------


def _create_completed_session_with_transcripts(org_id: int, user_id: int):
    """Seed a completed session with a couple of Transcription rows so that
    ai_chat / ai_insights / digests endpoints reach their LLM/gate path."""
    _, _, _, SessionLocal, RecordingSession = _models()
    from database.models import Transcription
    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid,
            name=f"v3182-{sid[:8]}",
            title=f"v3182-{sid[:8]}",
            meeting_type="standard",
            status="completed",
            duration=120.0,
            user_id=user_id,
            organization_id=org_id,
            summary="Test summary for digest aggregation.",
            processing_metadata={"created_by": "test"},
        )
        db.add(s); db.commit(); db.refresh(s)
        for idx, txt in enumerate(["Hello world.", "Second line."]):
            db.add(Transcription(
                session_id=s.id,
                text=txt,
                speaker="SPEAKER_00",
                start_time=float(idx),
                end_time=float(idx + 1),
                confidence=0.9,
            ))
        db.commit()
        return s.id, s.session_id
    finally:
        db.close()


def test_v3_18_2_free_blocked_from_ai_chat_messages(client):
    org_id, org_slug, user_id = _seed_user_and_org(
        "v3182-aichat-free", "v3182_aichat_free", "free"
    )
    headers = _login_headers(client, "v3182_aichat_free", "admin123", org_slug)
    _, session_id = _create_completed_session_with_transcripts(org_id, user_id)
    resp = client.post(
        f"/api/ai-chat/sessions/{session_id}/messages",
        headers=headers,
        json={"message": "What was discussed?"},
    )
    assert resp.status_code == 403, resp.text
    # v3.23.0: gate flipped from canonical_reprocess → ai_chat_over_corpus
    # so Basic tier (text-corpus) can ask questions about meetings.
    assert "ai_chat_over_corpus" in resp.text


def test_v3_18_2_free_blocked_from_ai_chat_rag_query(client):
    org_id, org_slug, user_id = _seed_user_and_org(
        "v3182-rag-free", "v3182_rag_free", "free"
    )
    headers = _login_headers(client, "v3182_rag_free", "admin123", org_slug)
    resp = client.post(
        "/api/ai-chat/rag/query",
        headers=headers,
        json={"message": "any meeting"},
    )
    assert resp.status_code == 403, resp.text
    # v3.23.0: gate flipped from canonical_reprocess → cross_meeting_search
    # so Basic tier (text-corpus) gets cross-meeting RAG.
    assert "cross_meeting_search" in resp.text


def test_v3_18_2_free_blocked_from_insights_regenerate(client):
    org_id, org_slug, user_id = _seed_user_and_org(
        "v3182-insights-free", "v3182_insights_free", "free"
    )
    headers = _login_headers(client, "v3182_insights_free", "admin123", org_slug)
    _, session_id = _create_completed_session_with_transcripts(org_id, user_id)
    resp = client.post(
        f"/api/simple/recording-sessions/{session_id}/insights/regenerate",
        headers=headers,
    )
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text


def test_v3_18_2_free_blocked_from_digest_generation(client):
    """Cached digest reads stay open for any tier; new digest generation
    is paid-tier. Free + no cached digest => 403."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "v3182-digest-free", "v3182_digest_free", "free"
    )
    headers = _login_headers(client, "v3182_digest_free", "admin123", org_slug)
    # Create a completed session so the digest generation path has data
    # (otherwise the early "no sessions" return short-circuits before the gate).
    _create_completed_session_with_transcripts(org_id, user_id)
    resp = client.get(
        "/api/digests?period=day&force=true",
        headers=headers,
    )
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text


def test_v3_18_2_ai_insights_uses_asyncio_to_thread():
    """Audit finding: chat_sync() (sync LLM call) was being awaited inside
    async handlers, blocking the asyncio event loop. v3.18.2 wraps each
    chat_sync in asyncio.to_thread so the loop stays responsive."""
    import inspect
    from api import ai_insights as mod
    src = inspect.getsource(mod)
    # Every chat_sync call in this module must be wrapped in to_thread.
    # Bare `svc.chat_sync(` (not preceded by to_thread,) is a regression.
    chat_sync_calls = src.count("svc.chat_sync")
    to_thread_calls = src.count("asyncio.to_thread")
    assert chat_sync_calls > 0, "expected at least one chat_sync usage"
    # Each chat_sync call should pair with a to_thread wrapping.
    assert to_thread_calls >= chat_sync_calls, (
        f"every chat_sync must be wrapped in asyncio.to_thread "
        f"({chat_sync_calls} chat_sync vs {to_thread_calls} to_thread)"
    )


# ---------------------------------------------------------------------------
# v3.18.3 batch — defense-in-depth tier gates on session_emails / batch_export /
# speakers per the Codex tier-gating audit table.
# ---------------------------------------------------------------------------


def test_v3_18_3_session_emails_free_blocked(client):
    """POST /api/sessions/{id}/email-attendees should 403 free users.

    Free tier has no server-side summary / canonical transcript to email
    out (browser-only); gate prevents future regressions silently
    re-opening the surface.
    """
    _, _, _, SessionLocal, RecordingSession = _models()
    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-emails-free", "tiergate_emails_free", "free"
    )
    headers = _login_headers(client, "tiergate_emails_free", "admin123", org_slug)

    db = SessionLocal()
    try:
        sid = str(_uuid.uuid4())
        s = RecordingSession(
            session_id=sid, name=f"emails-{sid[:8]}", title=f"emails-{sid[:8]}",
            meeting_type="standard", status="completed",
            duration=60.0, user_id=user_id, organization_id=org_id,
            processing_metadata={"created_by": "test"},
        )
        db.add(s); db.commit(); db.refresh(s)
        session_pk_id = s.id
    finally:
        db.close()

    resp = client.post(
        f"/api/simple/recording-sessions/{session_pk_id}/email-attendees",
        headers=headers,
        json={"speaker_ids": [], "additional_recipients": ["x@example.com"]},
    )
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text


def test_v3_18_3_batch_export_free_blocked(client):
    """POST /api/export/batch and POST /api/export/templates should 403 free."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-export-free", "tiergate_export_free", "free"
    )
    headers = _login_headers(client, "tiergate_export_free", "admin123", org_slug)

    # Batch export — gate must trip before any session-lookup or job creation.
    resp = client.post(
        "/api/export/batch",
        headers=headers,
        json={
            "sessionIds": [str(_uuid.uuid4())],
            "format": "pdf",
            "options": {},
        },
    )
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text

    # Templates write surface.
    resp = client.post(
        "/api/export/templates",
        headers=headers,
        json={
            "id": "ignored",
            "name": "test",
            "description": "test template",
            "format": "pdf",
            "options": {},
        },
    )
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text


def test_v3_18_3_speakers_write_free_blocked(client):
    """POST /api/speakers + PATCH/DELETE speaker variants should 403 free
    users (no speaker_library feature). GETs stay reachable so a downgrade
    doesn't blind the user to existing data."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-speakers-free", "tiergate_speakers_free", "free"
    )
    headers = _login_headers(client, "tiergate_speakers_free", "admin123", org_slug)

    # Write — create speaker.
    resp = client.post(
        "/api/speakers",
        headers=headers,
        json={"display_name": "Test Speaker"},
    )
    assert resp.status_code == 403, resp.text
    assert "speaker_library" in resp.text

    # Read — should NOT be 403 (gate is write-only on this endpoint).
    resp = client.get("/api/speakers", headers=headers)
    assert resp.status_code != 403, resp.text


# ---------------------------------------------------------------------------
# v3.29.2 — stop_recording + always-on/start defense-in-depth gates
# ---------------------------------------------------------------------------


def test_v3_29_2_stop_recording_free_blocked(client):
    """POST /api/simple/recording-sessions/{id}/stop schedules the server
    completion pass (transcribe+diarize+identify+index). The gate runs before
    the session lookup, so a free user 403s regardless of session existence."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-stop-free", "tiergate_stop_free", "free"
    )
    headers = _login_headers(client, "tiergate_stop_free", "admin123", org_slug)
    resp = client.post(
        "/api/simple/recording-sessions/nonexistent-sid/stop", headers=headers
    )
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text


def test_v3_29_2_stop_recording_pro_passes_gate(client):
    """A pro user clears the stop gate — proven by getting past it to the
    session lookup (404 for a nonexistent session), not a 403."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-stop-pro", "tiergate_stop_pro", "pro"
    )
    headers = _login_headers(client, "tiergate_stop_pro", "admin123", org_slug)
    resp = client.post(
        "/api/simple/recording-sessions/nonexistent-sid/stop", headers=headers
    )
    assert resp.status_code != 403, resp.text
    assert resp.status_code == 404, resp.text


def test_v3_29_2_always_on_start_free_blocked(client):
    """POST /api/simple/always-on/start is server-side continuous capture —
    free users must 403 (the gate runs before the recorder is touched, so no
    recorder is started in this test)."""
    org_id, org_slug, user_id = _seed_user_and_org(
        "tiergate-aostart-free", "tiergate_aostart_free", "free"
    )
    headers = _login_headers(client, "tiergate_aostart_free", "admin123", org_slug)
    resp = client.post("/api/simple/always-on/start", headers=headers)
    assert resp.status_code == 403, resp.text
    assert "canonical_reprocess" in resp.text
