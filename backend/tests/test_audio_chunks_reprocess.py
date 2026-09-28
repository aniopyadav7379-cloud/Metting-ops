"""Tests for the full-session audio capture + server-side reprocess pipeline.

Covers the endpoints landed alongside the live-summary fix:
  * POST /api/recordings/sessions/{id}/audio-chunks — accepts, persists to
    disk, records arrival metadata under processing_metadata.full_audio.
  * POST /api/recordings/sessions/{id}/audio-chunks — idempotent on
    chunk_index (re-upload overwrites without duplicating state).
  * POST /api/recordings/sessions/{id}/finalize-audio — when no chunks
    were ever uploaded (privacy mode), returns reprocessing_started=False
    and does NOT touch transcript_simple. This pins the privacy guarantee:
    a local-only session that never POSTed audio cannot trigger a
    server-side reprocess.
  * POST /api/recordings/sessions/{id}/finalize-audio — with chunks
    present, queues the reprocess. We monkeypatch the BackgroundTasks
    submission so we can assert the queueing happened without driving
    the full STT/diar/LLM pipeline in unit tests.

These tests are NOT designed to validate the end-to-end pipeline (that
requires the cloud Parakeet + speaker-svc + Qwen providers). They pin
the wire contract + side-effects on the FastAPI surface.
"""
from __future__ import annotations

import uuid as _uuid
from datetime import timedelta
from unittest.mock import patch

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


def _seed_user_and_org(slug: str, username: str = "audiocap_admin"):
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


def _create_always_on_session(org_id: int, user_id: int) -> tuple[int, str]:
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        session_uuid = str(_uuid.uuid4())
        session = RecordingSession(
            session_id=session_uuid,
            name=f"audio-cap-test-{session_uuid[:8]}",
            title=f"audio-cap-test-{session_uuid[:8]}",
            description="audio capture + reprocess test",
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


def _read_full_audio_state(session_pk: int) -> dict:
    _, _, _, SessionLocal, RecordingSession = _models()
    db = SessionLocal()
    try:
        row = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        assert row is not None
        meta = dict(row.processing_metadata or {})
        return dict(meta.get("full_audio") or {})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. Happy path — chunk uploads land on disk + state mirrors arrival
# ---------------------------------------------------------------------------
def test_audio_chunks_upload_records_state(client, tmp_path, monkeypatch):
    """Uploading a chunk persists to disk under the per-session full_audio
    directory and updates processing_metadata.full_audio.chunks to record
    arrival. The chunk count matches the number of distinct chunk_indices."""
    # Redirect the recordings dir into pytest's tmp_path so the test
    # filesystem doesn't pollute the real /app/recordings on the host.
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("audiocap-happy")
    headers = _login_headers(client, "audiocap_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    payload_0 = b"\x1a\x45\xdf\xa3" + b"\x00" * 64  # WebM EBML header bytes + filler
    resp = client.post(
        f"/api/recordings/sessions/{session_id}/audio-chunks",
        headers=headers,
        files={"chunk": ("chunk-0.webm", payload_0, "audio/webm")},
        data={"chunk_index": "0"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["chunk_index"] == 0
    assert body["size"] == len(payload_0)
    assert body["chunks_received"] == 1

    state = _read_full_audio_state(session_pk)
    assert "0" in (state.get("chunks") or {})
    assert state["chunks"]["0"]["size"] == len(payload_0)
    assert state["chunks"]["0"]["content_type"] == "audio/webm"

    # File actually lives on disk under the expected path.
    chunk_dir = tmp_path / "always_on" / org_slug / session_id / "full_audio"
    assert chunk_dir.exists()
    on_disk = sorted(p.name for p in chunk_dir.iterdir() if p.is_file())
    assert on_disk == ["000000.webm"]


# ---------------------------------------------------------------------------
# 2. Idempotent re-upload — same chunk_index overwrites without duplication
# ---------------------------------------------------------------------------
def test_audio_chunks_idempotent_on_index(client, tmp_path, monkeypatch):
    """Re-uploading the same chunk_index overwrites the existing file and
    does NOT create a second entry under chunks[idx]. The retry-on-failure
    path on the frontend depends on this guarantee — without it, a late-ACK
    network blip would double-write the audio."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("audiocap-idem")
    headers = _login_headers(client, "audiocap_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    # First send.
    r1 = client.post(
        f"/api/recordings/sessions/{session_id}/audio-chunks",
        headers=headers,
        files={"chunk": ("chunk-2.webm", b"FIRST_VERSION_BYTES", "audio/webm")},
        data={"chunk_index": "2"},
    )
    assert r1.status_code == 200, r1.text

    # Retry — same index, different bytes.
    r2 = client.post(
        f"/api/recordings/sessions/{session_id}/audio-chunks",
        headers=headers,
        files={"chunk": ("chunk-2.webm", b"SECOND_VERSION_BYTES_LONGER", "audio/webm")},
        data={"chunk_index": "2"},
    )
    assert r2.status_code == 200, r2.text

    state = _read_full_audio_state(session_pk)
    chunks = state.get("chunks") or {}
    # Only ONE entry for index 2 — the second write replaced the first.
    assert list(chunks.keys()) == ["2"]
    assert chunks["2"]["size"] == len(b"SECOND_VERSION_BYTES_LONGER")

    # And the on-disk file holds the second-version bytes, not the first.
    chunk_dir = tmp_path / "always_on" / org_slug / session_id / "full_audio"
    files = list(chunk_dir.iterdir())
    assert len(files) == 1, f"Expected exactly one file per index, got {files}"
    assert files[0].read_bytes() == b"SECOND_VERSION_BYTES_LONGER"


# ---------------------------------------------------------------------------
# 3. Privacy mode — finalize without chunks does NOT queue reprocess
# ---------------------------------------------------------------------------
def test_finalize_audio_with_no_chunks_skips_reprocess(client, tmp_path, monkeypatch):
    """When the user recorded in privacy/local-only mode, no audio chunks
    ever leave the device. finalize-audio in that case must return
    reprocessing_started=False and refuse to queue the pipeline — the
    live transcript stays canonical and no server-side STT runs."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("audiocap-nochunks")
    headers = _login_headers(client, "audiocap_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    # Make sure the helper would refuse to actually do work — patch the
    # background runner so we get a hard assertion if we accidentally
    # queued anything.
    with patch.object(recording, "_run_session_reprocess") as queued:
        resp = client.post(
            f"/api/recordings/sessions/{session_id}/finalize-audio",
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reprocessing_started"] is False
    assert body["reason"] == "no_chunks_uploaded"

    # Background task must NOT have been scheduled — the runner reference
    # captured by BackgroundTasks would have been our patched mock if so.
    queued.assert_not_called()

    state = _read_full_audio_state(session_pk)
    assert state.get("status") == "skipped"
    assert state.get("skipped_reason") == "no_chunks_uploaded"


# ---------------------------------------------------------------------------
# 4. Finalize after chunk upload sets reprocess_status=queued
# ---------------------------------------------------------------------------
def test_finalize_audio_with_chunks_queues_reprocess(client, tmp_path, monkeypatch):
    """When at least one chunk was uploaded, finalize-audio writes
    reprocess_status='queued' to processing_metadata and returns
    reprocessing_started=True. We patch the background runner so the
    test doesn't try to actually exercise the Parakeet/diar/LLM pipeline."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("audiocap-queue")
    headers = _login_headers(client, "audiocap_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    # Drop a single chunk so finalize doesn't skip.
    up = client.post(
        f"/api/recordings/sessions/{session_id}/audio-chunks",
        headers=headers,
        files={"chunk": ("chunk-0.webm", b"\x1a\x45\xdf\xa3" + b"\x00" * 32, "audio/webm")},
        data={"chunk_index": "0"},
    )
    assert up.status_code == 200, up.text

    # Stub out the background pipeline so the test doesn't try to actually
    # run ffmpeg + STT + LLM. We replace the runner before finalize so
    # FastAPI's BackgroundTasks captures the stubbed callable instead.
    async def _noop(_session_pk: int) -> None:  # pragma: no cover — async stub
        return None

    monkeypatch.setattr(recording, "_run_session_reprocess", _noop)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/finalize-audio",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reprocessing_started"] is True
    # Phase A.5: response status is 'complete' (a request-level
    # outcome — server has everything it needs and queued the
    # pipeline). The per-row audio_state status mirrors the
    # background-task lifecycle ('queued' → 'in_progress' →
    # 'complete') so checking the row state stays the right way
    # to assert what the pipeline is doing.
    assert body["status"] == "complete"

    state = _read_full_audio_state(session_pk)
    assert state.get("status") == "queued"


# ---------------------------------------------------------------------------
# 5. Sad path — audio-chunks for a wrong-mode session returns 409
# ---------------------------------------------------------------------------
def test_audio_chunks_rejects_non_always_on_session(client, tmp_path, monkeypatch):
    """audio-chunks is scoped to always-on sessions. Posting to an upload-
    mode session returns 409 — same wire contract as the existing
    /chunks-text endpoint, so the frontend's error handling stays uniform."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    Organization, User, UserOrganization, SessionLocal, RecordingSession = _models()
    org_id, org_slug, user_id = _seed_user_and_org("audiocap-wrongmode")
    headers = _login_headers(client, "audiocap_admin", "admin123", org_slug)

    # Create a session with mode='upload' to assert the guard fires.
    db = SessionLocal()
    try:
        uid = str(_uuid.uuid4())
        bad = RecordingSession(
            session_id=uid,
            name="upload-mode-session",
            title="upload-mode-session",
            mode="upload",
            status="recording",
            duration=0.0,
            user_id=user_id,
            organization_id=org_id,
            source_type="upload",
        )
        db.add(bad)
        db.commit()
        db.refresh(bad)
        session_id = bad.session_id
    finally:
        db.close()

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/audio-chunks",
        headers=headers,
        files={"chunk": ("chunk-0.webm", b"x" * 16, "audio/webm")},
        data={"chunk_index": "0"},
    )
    assert resp.status_code == 409, resp.text
    assert "not an always-on session" in resp.text.lower()


# ===========================================================================
# Phase A.5 — desktop local-first verification + /full-audio fallback
# ===========================================================================


def _sha256_hex(data: bytes) -> str:
    """Compute the same SHA-256 the browser client passes in the
    verification payload (raw chunk-bytes concat, no fancy framing)."""
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _upload_chunks_sequence(client, session_id, headers, payloads):
    """Helper — POST each (index, bytes) pair to /audio-chunks and
    return the assembled byte string (in upload order) for use as
    the client-side verification SHA payload."""
    assembled = b""
    for idx, payload in payloads:
        resp = client.post(
            f"/api/recordings/sessions/{session_id}/audio-chunks",
            headers=headers,
            files={"chunk": (f"chunk-{idx}.webm", payload, "audio/webm")},
            data={"chunk_index": str(idx)},
        )
        assert resp.status_code == 200, resp.text
        assembled += payload
    return assembled


# ---------------------------------------------------------------------------
# 6. Verification — matching client payload returns status=complete
# ---------------------------------------------------------------------------
def test_finalize_audio_verification_match_returns_complete(client, tmp_path, monkeypatch):
    """Client computes a SHA-256 + byte count + chunk count from its
    IndexedDB mirror and POSTs them with finalize-audio. When all three
    match the server's on-disk state, server returns status='complete'
    and queues the reprocess pipeline as usual."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("audiocap-verify-match")
    headers = _login_headers(client, "audiocap_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    payloads = [
        (0, b"\x1a\x45\xdf\xa3" + b"AAA" * 8),
        (1, b"\x1a\x45\xdf\xa3" + b"BBB" * 8),
        (2, b"\x1a\x45\xdf\xa3" + b"CCC" * 8),
    ]
    assembled = _upload_chunks_sequence(client, session_id, headers, payloads)

    # Don't actually run the background pipeline.
    async def _noop(_pk: int) -> None:
        return None

    monkeypatch.setattr(recording, "_run_session_reprocess", _noop)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/finalize-audio",
        headers=headers,
        json={
            "client_chunk_count": len(payloads),
            "client_bytes_total": len(assembled),
            "client_sha256": _sha256_hex(assembled),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "complete"
    assert body["reprocessing_started"] is True
    state = _read_full_audio_state(session_pk)
    assert state.get("verification", {}).get("matched") is True
    assert state.get("status") == "queued"


# ---------------------------------------------------------------------------
# 7. Verification — mismatched byte count returns status=incomplete
# ---------------------------------------------------------------------------
def test_finalize_audio_verification_mismatch_returns_incomplete(client, tmp_path, monkeypatch):
    """If the client's byte count + SHA-256 don't match what's on the
    server, /finalize-audio refuses to queue the reprocess and returns
    status='incomplete' plus the list of chunk indices actually present.
    The client uses this to either backfill specific chunks or fall
    back to /full-audio."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("audiocap-verify-mismatch")
    headers = _login_headers(client, "audiocap_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    # Server gets chunks 0 and 2 — chunk 1 is missing from the upload.
    server_payloads = [
        (0, b"FIRST_CHUNK_DATA"),
        (2, b"THIRD_CHUNK_DATA"),
    ]
    _upload_chunks_sequence(client, session_id, headers, server_payloads)

    # The "client" claims 3 chunks landed with a totally different
    # SHA — we expect status='incomplete' and missing_chunks=[1].
    fake_sha = "0" * 64
    queued = []

    async def _trap(_pk: int) -> None:
        queued.append(_pk)

    monkeypatch.setattr(recording, "_run_session_reprocess", _trap)

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/finalize-audio",
        headers=headers,
        json={
            "client_chunk_count": 3,
            "client_bytes_total": 999,
            "client_sha256": fake_sha,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "incomplete"
    assert body["reprocessing_started"] is False
    assert body["server_chunks"] == [0, 2]
    assert body["missing_chunks"] == [1]
    assert body["expected_chunks"] == 3
    # Reprocess must NOT have queued — server caught the mismatch.
    assert queued == [], "Reprocess queued despite verification mismatch"
    state = _read_full_audio_state(session_pk)
    assert state.get("status") == "incomplete"
    assert state.get("verification", {}).get("matched") is False


# ---------------------------------------------------------------------------
# 8. /full-audio — whole-file fallback replaces chunk state + queues reprocess
# ---------------------------------------------------------------------------
def test_full_audio_replaces_existing_chunks(client, tmp_path, monkeypatch):
    """When per-chunk recovery isn't viable, the client POSTs the
    assembled blob to /full-audio. Endpoint clears any existing chunk
    files, writes the recovery blob as the canonical session audio,
    and queues the same reprocess pipeline /finalize-audio runs.
    Cross-org leak guard sits a few tests below."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("audiocap-fullaudio")
    headers = _login_headers(client, "audiocap_admin", "admin123", org_slug)
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    # Seed a couple of pre-existing chunks (the failed-upload state).
    _upload_chunks_sequence(
        client,
        session_id,
        headers,
        [
            (0, b"OLD_CHUNK_0"),
            (1, b"OLD_CHUNK_1"),
        ],
    )

    queued = []

    async def _trap(pk: int) -> None:
        queued.append(pk)

    monkeypatch.setattr(recording, "_run_session_reprocess", _trap)

    recovery_blob = b"\x1a\x45\xdf\xa3" + b"WHOLE_FILE_RECOVERY_DATA" * 64
    resp = client.post(
        f"/api/recordings/sessions/{session_id}/full-audio",
        headers=headers,
        files={"audio": ("full.webm", recovery_blob, "audio/webm")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "complete"
    assert body["reprocessing_started"] is True
    assert body["bytes_received"] == len(recovery_blob)
    assert body["source"] == "full_audio_fallback"

    # Reprocess queued — captured by our trap.
    assert queued == [session_pk]

    # Existing chunks were cleared; only the recovery blob remains.
    chunk_dir = tmp_path / "always_on" / org_slug / session_id / "full_audio"
    on_disk = sorted(p.name for p in chunk_dir.iterdir() if p.is_file())
    # _safe_ext sees content_type=audio/webm so .webm is the ext.
    assert on_disk == ["000000.webm"], f"Expected single recovery file, got {on_disk}"
    assert (chunk_dir / "000000.webm").read_bytes() == recovery_blob

    state = _read_full_audio_state(session_pk)
    assert state.get("source") == "full_audio_fallback"
    assert state.get("status") == "queued"


# ---------------------------------------------------------------------------
# 9. /full-audio cross-org isolation — user A can't POST to user B's session
# ---------------------------------------------------------------------------
def test_full_audio_rejects_cross_org_post(client, tmp_path, monkeypatch):
    """Same isolation guarantee /audio-chunks ships with: a user in
    org A who learns the session_id of a row in org B (via guessing,
    log scraping, whatever) cannot POST recovery audio against it.
    The 404 lookup-by-org wall must hold."""
    from api import recording

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    # Org A owns the session.
    org_a_id, org_a_slug, user_a_id = _seed_user_and_org("audiocap-org-a", username="user_a")
    _, session_id = _create_always_on_session(org_a_id, user_a_id)

    # Org B has a separate user trying to write to that session.
    _seed_user_and_org("audiocap-org-b", username="user_b")
    headers_b = _login_headers(client, "user_b", "admin123", "audiocap-org-b")

    resp = client.post(
        f"/api/recordings/sessions/{session_id}/full-audio",
        headers=headers_b,
        files={"audio": ("evil.webm", b"x" * 32, "audio/webm")},
    )
    assert resp.status_code == 404, resp.text
    # We deliberately give the same 404 we give for missing sessions
    # — never leak the cross-org existence signal.
    assert "session not found" in resp.text.lower()


# ---------------------------------------------------------------------------
# 10. Reprocess preflight — reject corrupt/duplicated assembly before GPUs
# ---------------------------------------------------------------------------
def test_reprocess_preflight_rejects_implausible_duration_and_keeps_transcript(
    client, tmp_path, monkeypatch
):
    """A 25-minute session decoded as hours must fail before STT/diarization.

    The old browser transcript remains intact so the user can choose whole-file
    recovery rather than losing the only readable meeting record.
    """
    from api import recording
    from database.database import SessionLocal
    from database.models import RecordingSession
    import asyncio

    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")
    org_id, org_slug, user_id = _seed_user_and_org("audiocap-preflight-duration")
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).one()
        now = recording._now()
        session.started_at = now - timedelta(minutes=25)
        session.ended_at = now
        session.duration = 25 * 60
        session.status = "processing"
        session.transcript_simple = "Keep this existing browser transcript."
        db.commit()
    finally:
        db.close()

    chunks_dir = tmp_path / "always_on" / org_slug / session_id / "full_audio"
    chunks_dir.mkdir(parents=True)
    for index in range(3):
        (chunks_dir / f"{index:06d}.webm").write_bytes(f"unique-{index}".encode())

    async def _fake_reassemble(_chunks_dir, target_wav):
        target_wav.parent.mkdir(parents=True, exist_ok=True)
        target_wav.write_bytes(b"RIFF0000WAVE")
        return 4 * 60 * 60, "webm"

    monkeypatch.setattr(recording, "_reassemble_full_audio", _fake_reassemble)
    asyncio.run(recording._run_session_reprocess(session_pk))

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).one()
        audio_state = (session.processing_metadata or {}).get("full_audio") or {}
        assert session.transcript_simple == "Keep this existing browser transcript."
        assert audio_state["status"] == "failed"
        assert audio_state["audio_preflight"]["code"] == "duration_mismatch"
        assert "whole-file recovery" in audio_state["audio_preflight"]["message"]
    finally:
        db.close()


def test_reprocess_preflight_rejects_repeated_chunk_loop(tmp_path):
    """Exact repeated MediaRecorder fragments are a cheap, decisive guard."""
    from api import recording

    class _Session:
        duration = 0
        started_at = None
        ended_at = None

    chunks_dir = tmp_path / "full_audio"
    chunks_dir.mkdir()
    for index, payload in enumerate((b"A", b"A", b"A", b"B", b"C")):
        (chunks_dir / f"{index:06d}.webm").write_bytes(payload)

    with pytest.raises(recording.AudioPreflightError) as exc_info:
        recording._validate_reprocessed_audio(_Session(), chunks_dir, 150.0)
    assert exc_info.value.code == "duplicate_chunks"
    assert exc_info.value.details["duplicate_chunk_count"] == 2
