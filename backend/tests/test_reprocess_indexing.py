"""Regression guard for Stage-5.9 semantic indexing in the reprocess pipeline.

This is the highest-cost incident this codebase has had: the server-side
reprocess pipeline (``api.recording._run_session_reprocess``) ran STT ->
diarize -> identify -> summarize but NEVER indexed the result into Qdrant.
Only the legacy ``simple_recording_db`` finalize path indexed, so every
always-on finalize AND every upload produced a transcript/summary that was
invisible to cross-meeting search and RAG chat (sessions 496/499/502 had 0
points each). Stage 5.9 closed the gap by calling
``semantic_search.index_session`` after identify_speakers has rewritten the
diarized labels.

To pin that behaviour we run the REAL ``_run_session_reprocess`` (NOT mocked)
and stub ONLY the heavy leaf calls it invokes:

  * ``_reassemble_full_audio``                 (ffmpeg)
  * ``api.uploads._transcribe_audio``          (Parakeet)
  * the diarization provider's ``.diarize``    (speaker-svc)
  * ``identify_speakers`` / ``stamp_confirmed_speaker_contacts``
  * ``api.uploads._summarize_session``         (LLM)
  * ``api.ai_insights._generate_ai_insights``  (LLM)
  * ``services.session_media.persist_session_audio``  (Garage)
  * ``write_meeting_to_brigade``               (Brigade graph)
  * ``write_action_items_to_projectops``       (Project-Ops)
  * ``_autostamp_recorder_participant``        (Contact-Ops)

We SPY on ``semantic_search.index_session`` and assert it fired exactly once
with a non-empty transcript and the right session_id + organization_id.

DB side-effects use the real SQLite fixture from conftest.py; only the leaf
HTTP/LLM/ffmpeg calls are stubbed. Monkeypatch style mirrors
test_audio_chunks_reprocess.py.
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


def _seed_user_and_org(slug: str, username: str = "reprocidx_admin"):
    Organization, User, UserOrganization, SessionLocal, _ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True)
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
                tier="enterprise",
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
            name=f"reproc-idx-{session_uuid[:8]}",
            title=f"reproc-idx-{session_uuid[:8]}",
            description="reprocess semantic-index regression test",
            meeting_type="always_on",
            mode="always_on",
            status="processing",
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


# ---------------------------------------------------------------------------
# The regression test
# ---------------------------------------------------------------------------
def test_reprocess_indexes_to_semantic_search(client, tmp_path, monkeypatch):
    """Running the real reprocess pipeline must call
    ``semantic_search.index_session`` exactly once, with the diarized
    transcript and the correct session_id + organization_id. If a future
    refactor drops Stage 5.9 again, this test goes red instead of search
    silently emptying out."""
    from api import recording
    import api.uploads as uploads_mod
    import api.ai_insights as ai_insights_mod
    import services.providers.registry as registry_mod
    import services.speaker_service as speaker_service_mod
    import services.session_media as session_media_mod
    import services.brigade_writer as brigade_writer_mod
    import services.projectops_writer as projectops_writer_mod
    from services.semantic_search_service import semantic_search

    # Sandbox every on-disk path into pytest's tmp_path.
    monkeypatch.setattr(recording, "RECORDINGS_DIR", tmp_path)
    monkeypatch.setattr(recording, "ALWAYS_ON_DIR", tmp_path / "always_on")

    org_id, org_slug, user_id = _seed_user_and_org("reprocidx-org")
    session_pk, session_id = _create_always_on_session(org_id, user_id)

    # --- stub the heavy leaf calls -----------------------------------------

    async def _fake_reassemble(chunks_dir, target_wav):
        # The pipeline reads target_wav.stat().st_size right after this call,
        # so the stub must materialise the file on disk.
        target_wav.parent.mkdir(parents=True, exist_ok=True)
        target_wav.write_bytes(b"RIFF0000WAVE")
        return 12.5, "webm"

    monkeypatch.setattr(recording, "_reassemble_full_audio", _fake_reassemble)

    async def _fake_transcribe(audio_path, organization_id, db, *, language="en", **_kw):
        return {
            "segments": [
                {"text": "Kickoff for the Atlas migration.", "speaker": "SPEAKER_00",
                 "start": 0.0, "end": 3.0, "confidence": 0.97},
                {"text": "We ship the cutover next Friday.", "speaker": "SPEAKER_01",
                 "start": 3.0, "end": 6.0, "confidence": 0.96},
            ],
            "model": "parakeet-tdt-1.1b",
            "language": "en",
        }

    monkeypatch.setattr(uploads_mod, "_transcribe_audio", _fake_transcribe)

    # Diarization provider — return overlapping turns with embeddings so the
    # overlay path runs, but it's not what we assert on here.
    class _FakeDiarProvider:
        async def diarize(self, _wav_path):
            return [
                {"start": 0.0, "end": 3.0, "speaker": "SPEAKER_00", "embedding": [0.1, 0.2]},
                {"start": 3.0, "end": 6.0, "speaker": "SPEAKER_01", "embedding": [0.3, 0.4]},
            ]

    class _FakeRegistry:
        def get_diarization(self, _org_id):
            return _FakeDiarProvider()

    monkeypatch.setattr(registry_mod, "get_provider_registry", lambda _db: _FakeRegistry())

    # identify_speakers / stamp are sync (called via asyncio.to_thread) — no-op.
    monkeypatch.setattr(speaker_service_mod, "identify_speakers", lambda session, db: None)
    monkeypatch.setattr(
        speaker_service_mod, "stamp_confirmed_speaker_contacts", lambda session, db: None
    )

    # Garage persist — best-effort in prod; stub so no object-store call.
    monkeypatch.setattr(session_media_mod, "persist_session_audio", lambda db, session, local_path=None: None)

    async def _fake_summarize(db, session, template="standard"):
        session.summary = "Atlas migration kickoff; cutover ships next Friday."
        return None

    monkeypatch.setattr(uploads_mod, "_summarize_session", _fake_summarize)

    class _FakeInsights:
        def model_dump(self):
            return {"keywords": ["atlas", "cutover"], "action_items": []}

    async def _fake_insights(transcript, transcriptions, session, db, org_id):
        return _FakeInsights()

    monkeypatch.setattr(ai_insights_mod, "_generate_ai_insights", _fake_insights)

    # Downstream fan-out writers — best-effort in prod; stub to no-ops.
    async def _noop_brigade(session_pk, db):
        return None

    monkeypatch.setattr(brigade_writer_mod, "write_meeting_to_brigade", _noop_brigade)

    async def _noop_po(*, db, session_pk, completion_mode):
        return None

    monkeypatch.setattr(projectops_writer_mod, "write_action_items_to_projectops", _noop_po)

    async def _noop_autostamp(_session_pk):
        return None

    monkeypatch.setattr(recording, "_autostamp_recorder_participant", _noop_autostamp)

    # --- SPY on the semantic index (the thing under test) ------------------
    captured: dict = {}

    def _spy_index_session(*, session_id, title, transcript, summary, created_at, organization_id):
        captured["calls"] = captured.get("calls", 0) + 1
        captured["session_id"] = session_id
        captured["title"] = title
        captured["transcript"] = transcript
        captured["summary"] = summary
        captured["organization_id"] = organization_id

    monkeypatch.setattr(semantic_search, "index_session", _spy_index_session)

    # --- run the REAL reprocess pipeline -----------------------------------
    import asyncio

    asyncio.run(recording._run_session_reprocess(session_pk))

    # Stage 5.9 must have fired exactly once.
    assert captured.get("calls") == 1, (
        f"semantic_search.index_session was called {captured.get('calls')} time(s); "
        "the reprocess pipeline must index to Qdrant exactly once (Stage 5.9)."
    )
    # Indexed with the canonical session id + the owning org.
    assert captured["session_id"] == session_id
    assert captured["organization_id"] == org_id
    # The indexed transcript is the diarized text, not empty — an empty
    # transcript was the exact failure mode (search/RAG silently empty).
    assert captured["transcript"] and captured["transcript"].strip()
    assert "Atlas migration" in captured["transcript"]
    # Diarized speaker labels are prefixed onto each line — and since
    # v3.33.0 normalization (Stage 4.5) runs BEFORE indexing, raw diarizer
    # codes must NOT reach the index: unmatched voices index as the humane
    # "Speaker N" labels the UI + summary show (label parity, audit #3).
    assert "Speaker 1:" in captured["transcript"]
    assert "SPEAKER_00" not in captured["transcript"]
    # Summary from the (stubbed) summarizer also flows through to the index.
    assert "cutover" in (captured["summary"] or "")
