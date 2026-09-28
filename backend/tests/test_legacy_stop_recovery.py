"""Restart-resilient legacy /stop + idempotent session creation.

The incident this pins: a ~2h always-on recording was lost when the backend
container was restarted MID-recording. On Stop, the legacy
``POST /api/simple/recording-sessions/{id}/stop`` called
``audio_service.stop_recording()``, which consults ONLY in-memory state
(``is_recording`` / ``current_session_id`` — wiped by the restart) and
returned ``(False, "Not recording this session")`` -> the endpoint raised
HTTP 500 and stamped ``status='error'``. The audio chunks were durably on
disk the whole time, but nothing read them. Total, silent loss.

The fix (backend half):
  * /stop NEVER 500s for "Not recording this session" anymore. When the
    in-memory state is gone it FINALIZES FROM WHATEVER SURVIVED on the host
    volume — the always-on chunk dir (-> canonical reprocess), else a
    half-written WAV (-> process_recording), else an honest ``no_audio`` 200.
  * The happy path (live in-memory ffmpeg) is byte-for-byte unchanged.
  * Session creation is idempotent on an optional ``client_session_key`` so a
    retried start/create can't spawn the duplicate-row pair the incident
    also produced.

Mirrors the chunk-dir monkeypatch + seed style of
``test_audio_chunks_reprocess.py`` and the lazy-model-import + auth-models
first style of ``test_summary_idempotency.py``.
"""
from __future__ import annotations

import uuid as _uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import RecordingSession
    return Organization, User, UserOrganization, SessionLocal, RecordingSession


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


def _seed_user_and_org(slug: str, username: str, tier: str = "enterprise"):
    Organization, User, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True)
            db.add(org)
            db.commit()
            db.refresh(org)
        # billing-1: align the org plan with the seeded user tier (server compute
        # now also requires the ACTIVE org's plan to cover the feature).
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
                is_active=True,
                is_verified=True,
                tier=tier,  # paid tier: server-processing endpoints are gated (v3.0.0)
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


def _create_always_on_session(org_id: int, user_id: int, status: str = "recording") -> tuple[int, str]:
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        session_uuid = str(_uuid.uuid4())
        session = RecordingSession(
            session_id=session_uuid,
            name=f"stoprec-{session_uuid[:8]}",
            title=f"stoprec-{session_uuid[:8]}",
            description="legacy stop recovery test",
            meeting_type="always_on",
            mode="always_on",
            status=status,
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


def _read_session(session_pk: int):
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        row = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        assert row is not None
        # Detach a plain snapshot of the fields we assert on so the caller
        # isn't holding a live session after we close the db.
        return {
            "status": row.status,
            "audio_file": row.audio_file,
            "duration": row.duration,
            "full_audio": dict((row.processing_metadata or {}).get("full_audio") or {}),
            "stop_outcome": (row.processing_metadata or {}).get("stop_outcome"),
        }
    finally:
        db.close()


def _reset_singleton():
    """Simulate a backend restart: a fresh WorkingAudioService singleton has
    no in-memory recording state (is_recording=False, current_session_id=None),
    exactly the condition that made the legacy /stop 500."""
    from services.working_audio_service import audio_service
    audio_service.is_recording = False
    audio_service.current_session_id = None
    audio_service.recording_process = None
    assert audio_service.is_recording is False
    assert audio_service.current_session_id is None


# ===========================================================================
# CENTERPIECE — the incident: restart mid-record, chunks on disk, Stop recovers
# ===========================================================================
def test_legacy_stop_recovers_from_always_on_chunks(client, tmp_path, monkeypatch):
    """The exact failure that lost a 2h recording. An always-on session is
    'recording', 3 chunks are durably on disk, then the backend "restarts"
    (singleton in-memory state wiped). Hitting the legacy /stop must NOT 500
    and must NOT set status='error' — it finalizes from the on-disk chunks:
    HTTP 200, recovered=True, recovered_from='always_on_chunks',
    status='processing', reprocess enqueued once with the session pk, and
    full_audio.source=='legacy_stop_recovery'."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("stoprec-chunks", "stoprec_chunks")
    headers = _login_headers(client, "stoprec_chunks", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    # Stream 3 chunks during "recording" so the on-disk full_audio dir is
    # populated exactly like the live always-on path would.
    for idx in range(3):
        payload = b"\x1a\x45\xdf\xa3" + bytes([idx]) * 64  # WebM-ish header + filler
        up = client.post(
            f"/api/recordings/sessions/{session_id}/audio-chunks",
            headers=headers,
            files={"chunk": (f"chunk-{idx}.webm", payload, "audio/webm")},
            data={"chunk_index": str(idx)},
        )
        assert up.status_code == 200, up.text
    chunk_dir = tmp_path / "always_on" / org_slug / session_id / "full_audio"
    assert len(list(chunk_dir.iterdir())) == 3

    # "Restart": wipe the in-memory recorder state so stop_recording() returns
    # (False, "Not recording this session") — the incident condition.
    _reset_singleton()

    # Patch the reprocess enqueue (local-imported inside the recovery helper)
    # so the test asserts the queue call without driving the real pipeline.
    enqueue = AsyncMock(return_value="job-1")
    with patch("workers.reprocess_workers.enqueue_reprocess", enqueue):
        resp = client.post(f"/api/simple/recording-sessions/{session_id}/stop", headers=headers)

    # The incident produced HTTP 500; the fix guarantees 200.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "processing"
    assert body["recovered"] is True
    assert body["recovered_from"] == "always_on_chunks"

    snap = _read_session(session_pk)
    assert snap["status"] == "processing"        # NOT 'error'
    assert snap["full_audio"].get("source") == "legacy_stop_recovery"

    enqueue.assert_awaited_once()
    # First positional arg is the session pk.
    assert enqueue.await_args.args[0] == session_pk


# ===========================================================================
# Recovery from a half-written disk WAV (no always-on chunks)
# ===========================================================================
def test_legacy_stop_recovers_from_disk_wav(client, tmp_path, monkeypatch):
    """A SIGKILL'd conference-room ffmpeg can leave a partial
    recording_{sid}_*.wav on disk with no always-on chunks. With the
    singleton state gone, /stop must recover from that WAV: 200,
    recovered_from='disk_wav', audio_file points at the WAV,
    status='processing', and process_recording is scheduled."""
    from api import recording
    from services import working_audio_service as was

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")
    # Point the WAV scan at tmp_path so we don't read the real recordings dir.
    monkeypatch.setattr(was.WorkingAudioService, "RECORDINGS_DIR", str(tmp_path))

    org_id, org_slug, user_id = _seed_user_and_org("stoprec-wav", "stoprec_wav")
    headers = _login_headers(client, "stoprec_wav", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    # Drop a non-empty partial WAV named the way ffmpeg names it.
    wav = tmp_path / f"recording_{session_id}_20260614_010101.wav"
    wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 256)

    _reset_singleton()

    sched = MagicMock()
    # Patch process_recording so the test doesn't run real STT/diar/LLM.
    monkeypatch.setattr("api.simple_recording_db.process_recording", sched)

    resp = client.post(f"/api/simple/recording-sessions/{session_id}/stop", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "processing"
    assert body["recovered"] is True
    assert body["recovered_from"] == "disk_wav"

    snap = _read_session(session_pk)
    assert snap["status"] == "processing"
    assert snap["audio_file"] is not None
    assert snap["audio_file"].endswith(".wav")
    # process_recording was scheduled as a background task (ran after response).
    sched.assert_called_once()


# ===========================================================================
# Nothing persisted (true browser-first / DISABLE_LOCAL_AUDIO no-capture)
# ===========================================================================
def test_legacy_stop_with_nothing_persisted_is_no_audio_not_500(client, tmp_path, monkeypatch):
    """When NOTHING was ever persisted (no chunks, no WAV) and the in-memory
    state is gone, /stop must return an honest 200 no_audio — NEVER a cryptic
    500, and NEVER status='error'. There is no data to lose; the user gets a
    clear message instead of the connection-reset dialog the incident showed."""
    from api import recording
    from services import working_audio_service as was

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")
    monkeypatch.setattr(was.WorkingAudioService, "RECORDINGS_DIR", str(tmp_path))

    org_id, org_slug, user_id = _seed_user_and_org("stoprec-empty", "stoprec_empty")
    headers = _login_headers(client, "stoprec_empty", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    _reset_singleton()

    # If recovery wrongly queued anything, this AsyncMock would record it.
    enqueue = AsyncMock()
    with patch("workers.reprocess_workers.enqueue_reprocess", enqueue):
        resp = client.post(f"/api/simple/recording-sessions/{session_id}/stop", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "no_audio"
    assert body["recovered"] is False

    snap = _read_session(session_pk)
    assert snap["status"] != "error"             # honest terminal, not a failure
    assert snap["stop_outcome"] == "no_audio"
    enqueue.assert_not_awaited()


# ===========================================================================
# Happy path UNCHANGED — live in-memory ffmpeg (conference-room / session-530)
# ===========================================================================
def test_legacy_stop_happy_path_unchanged(client, tmp_path, monkeypatch):
    """When stop_recording() returns (True, '<path>.wav') (live in-memory
    ffmpeg, e.g. Conference Room), /stop behaves byte-for-byte as before:
    200 with the legacy shape (status='processing', audio_file, duration,
    the legacy message), process_recording scheduled, and the recovery
    helper NEVER invoked. Guards the blast radius."""
    from api import recording
    from services import working_audio_service as was

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("stoprec-happy", "stoprec_happy")
    headers = _login_headers(client, "stoprec_happy", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    happy_wav = str(tmp_path / "live_recording.wav")
    (tmp_path / "live_recording.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    # Simulate a live recorder: stop_recording returns success.
    monkeypatch.setattr(
        was.audio_service, "stop_recording", lambda sid: (True, happy_wav)
    )
    sched = MagicMock()
    monkeypatch.setattr("api.simple_recording_db.process_recording", sched)

    # The recovery helper must NOT run on the happy path.
    with patch("api.simple_recording_db._recover_and_finalize_stop") as recov:
        resp = client.post(f"/api/simple/recording-sessions/{session_id}/stop", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "processing"
    assert body["message"] == "Recording stopped, processing transcription..."
    assert body["audio_file"].endswith("live_recording.wav")
    assert "duration" in body
    # No recovery markers leak onto the happy-path response.
    assert "recovered" not in body
    recov.assert_not_called()
    sched.assert_called_once()


# ===========================================================================
# Idempotent always-on start — same client_session_key reuses one row
# ===========================================================================
def test_start_always_on_idempotent_on_client_session_key(client, tmp_path, monkeypatch):
    """POST /api/recordings/start-always-on twice with the SAME
    client_session_key within the window returns the SAME session_id with
    idempotent_reuse=True, and exactly ONE always-on row exists for that key.
    A DIFFERENT key produces a second, distinct row (no false-merge)."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    _, _, _, SessionLocal, RecordingSession = _models()
    org_id, org_slug, user_id = _seed_user_and_org("stoprec-idem", "stoprec_idem")
    headers = _login_headers(client, "stoprec_idem", "admin123", org_slug)

    key = "client-key-" + _uuid.uuid4().hex
    r1 = client.post("/api/recordings/start-always-on", headers=headers,
                     json={"client_session_key": key})
    assert r1.status_code == 200, r1.text
    first = r1.json()
    assert first.get("idempotent_reuse") is False

    r2 = client.post("/api/recordings/start-always-on", headers=headers,
                     json={"client_session_key": key})
    assert r2.status_code == 200, r2.text
    second = r2.json()
    assert second["id"] == first["id"]
    assert second.get("idempotent_reuse") is True

    db = SessionLocal()
    try:
        meta_key = key
        rows = [
            s for s in db.query(RecordingSession)
            .filter(RecordingSession.organization_id == org_id,
                    RecordingSession.mode == "always_on").all()
            if (s.processing_metadata or {}).get("client_session_key") == meta_key
        ]
        assert len(rows) == 1, f"expected exactly one row for key, got {len(rows)}"

        # A DIFFERENT key -> a new distinct row.
        other = "client-key-" + _uuid.uuid4().hex
        r3 = client.post("/api/recordings/start-always-on", headers=headers,
                         json={"client_session_key": other})
        assert r3.status_code == 200, r3.text
        assert r3.json()["id"] != first["id"]
    finally:
        db.close()


# ===========================================================================
# Idempotent /api/simple create — same client_session_key reuses one row
# ===========================================================================
def test_simple_create_idempotent_on_client_session_key(client, tmp_path, monkeypatch):
    """POST /api/simple/recording-sessions twice with the SAME
    client_session_key within the window returns the SAME session with no
    second insert. A different key (or no key) inserts a fresh row."""
    _, _, _, SessionLocal, RecordingSession = _models()
    org_id, org_slug, user_id = _seed_user_and_org("stoprec-simpleidem", "stoprec_simpleidem")
    headers = _login_headers(client, "stoprec_simpleidem", "admin123", org_slug)

    key = "simple-key-" + _uuid.uuid4().hex
    r1 = client.post("/api/simple/recording-sessions", headers=headers,
                     json={"name": "Dup guard", "client_session_key": key})
    assert r1.status_code == 200, r1.text
    first_id = r1.json()["id"]

    r2 = client.post("/api/simple/recording-sessions", headers=headers,
                     json={"name": "Dup guard retry", "client_session_key": key})
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == first_id  # reused, not a second insert

    db = SessionLocal()
    try:
        rows = [
            s for s in db.query(RecordingSession)
            .filter(RecordingSession.organization_id == org_id).all()
            if (s.processing_metadata or {}).get("client_session_key") == key
        ]
        assert len(rows) == 1, f"expected one row for key, got {len(rows)}"
    finally:
        db.close()

    # No key -> always a fresh row (two such creates -> two distinct ids).
    a = client.post("/api/simple/recording-sessions", headers=headers, json={"name": "nokey-a"})
    b = client.post("/api/simple/recording-sessions", headers=headers, json={"name": "nokey-b"})
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["id"] != b.json()["id"]


# ===========================================================================
# finalize-audio is restart-agnostic (re-confirm: reads only on-disk chunks)
# ===========================================================================
def test_finalize_audio_restart_agnostic_from_chunks(client, tmp_path, monkeypatch):
    """Populate the chunk dir, wipe the singleton, then call /finalize-audio
    WITHOUT ever touching in-memory recorder state: it still queues the
    reprocess (reprocessing_started=True, status='complete'), proving the
    finalize primitive the recovery path leans on never reads memory."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("stoprec-finalize", "stoprec_finalize")
    headers = _login_headers(client, "stoprec_finalize", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    up = client.post(
        f"/api/recordings/sessions/{session_id}/audio-chunks",
        headers=headers,
        files={"chunk": ("c0.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 32, "audio/webm")},
        data={"chunk_index": "0"},
    )
    assert up.status_code == 200, up.text

    _reset_singleton()

    async def _noop(_session_pk: int) -> None:  # pragma: no cover — async stub
        return None

    monkeypatch.setattr(recording, "_run_session_reprocess", _noop)
    resp = client.post(f"/api/recordings/sessions/{session_id}/finalize-audio", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reprocessing_started"] is True
    assert body["status"] == "complete"
