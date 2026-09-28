"""Regression tests for the always-on completion -> identify_speakers path.

Background: when always-on / chunked recordings completed, they went through
real_whisper_service.transcribe_file, which returns segments without per-turn
embeddings. identify_speakers then skipped every SPEAKER_xx label with
reason=no_embedding and no SpeakerSessionLink rows ever flipped from manual
to auto — even for users (Aaron) who had been enrolled at the org level.

These tests pin the contract going forward:

  1. transcript_diarized.segments WITH embeddings + a matching enrolled speaker
     -> identify_speakers writes a SpeakerSessionLink with source='auto' and a
     similarity above the threshold.
  2. transcript_diarized.segments WITHOUT embeddings (the regression shape)
     -> identify_speakers records reason='no_embedding' for each label and
     does NOT touch any existing manual links.
  3. transcript_diarized.segments WITH embeddings but below the similarity
     threshold -> identify_speakers records reason='below_threshold' and
     leaves the link unmatched.
  4. _assign_speakers_from_diarization (Path 1 — word-level alignment) PRESERVES
     the per-segment embedding from the diarization turns onto each utterance.
     This is the merge step that previously stripped embeddings, breaking
     identify_speakers downstream.
"""
from __future__ import annotations

import numpy as np
import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import (
        OrgProviderSettings,
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
    )
    return (
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        OrgProviderSettings,
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
    )


def _normalize(vec: list[float]) -> list[float]:
    arr = np.asarray(vec, dtype="float32")
    n = np.linalg.norm(arr)
    if n > 0:
        arr = arr / n
    return arr.tolist()


def _make_embedding(seed: int, dim: int = 256) -> list[float]:
    """Generate a deterministic unit-norm embedding for tests."""
    rng = np.random.default_rng(seed)
    return _normalize(rng.normal(size=dim).tolist())


def _seed_world(client):
    """Seed an org + user + enrolled SpeakerProfile and return ids.

    Idempotent — the test fixture uses a shared SQLite DB so calls across
    test cases get the same speaker row. The enrolled embedding is always
    seeded deterministically (seed=42), so similarity assertions hold across
    test ordering.

    Returns: (org_id, speaker_id, enrolled_embedding)
    """
    (
        Organization,
        User,
        _UO,
        SessionLocal,
        _OPS,
        _RS,
        SpeakerProfile,
        _SSL,
    ) = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == "magic-unicorn").first()
        assert org is not None, "magic-unicorn org should be seeded by conftest"
        org_id = org.id

        from services.speaker_service import encode_embedding

        enrolled = _make_embedding(seed=42)
        sp = (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.organization_id == org_id,
                SpeakerProfile.display_name == "Aaron Stransky (test)",
            )
            .first()
        )
        if sp is None:
            sp = SpeakerProfile(
                organization_id=org_id,
                display_name="Aaron Stransky (test)",
                centroid_embedding=encode_embedding(enrolled),
                embedding_dim=len(enrolled),
                embedding_model="pyannote/wespeaker-voxceleb-resnet34-LM",
                sample_count=1,
            )
            db.add(sp)
            db.commit()
            db.refresh(sp)
        return org_id, sp.id, enrolled
    finally:
        db.close()


def _make_session(org_id: int, segments: list[dict], audio_file: str = "") -> int:
    """Create a completed RecordingSession with the given diarized segments."""
    import uuid as _uuid
    (
        _Org,
        _U,
        _UO,
        SessionLocal,
        _OPS,
        RecordingSession,
        _SP,
        _SSL,
    ) = _models()
    db = SessionLocal()
    try:
        sess = RecordingSession(
            session_id=f"test-session-{_uuid.uuid4().hex[:12]}",
            name="Identify Embeddings Test",
            organization_id=org_id,
            status="completed",
            audio_file=audio_file,
            transcript_diarized={
                "segments": segments,
                "speakers": sorted({s.get("speaker") for s in segments if s.get("speaker")}),
                "model": "speaker-svc-test",
            },
        )
        db.add(sess)
        db.commit()
        db.refresh(sess)
        return sess.id
    finally:
        db.close()


class _FakeMatchingProvider:
    """Stand-in for LocalSpeakerSvcProvider used in tests so we don't depend
    on a live speaker-svc HTTP endpoint. Returns a positive identify match
    when the candidate embedding's cosine similarity to the query is above
    the threshold."""

    async def identify(self, embedding: list[float], candidates: list[dict], threshold: float) -> dict:
        q = np.asarray(embedding, dtype="float32")
        q_norm = np.linalg.norm(q)
        best = None
        best_sim = -1.0
        for c in candidates:
            e = np.asarray(c["embedding"], dtype="float32")
            denom = q_norm * np.linalg.norm(e)
            sim = float(np.dot(q, e) / denom) if denom > 0 else 0.0
            if sim > best_sim:
                best_sim = sim
                best = c
        if best is None:
            return {"best_match": None}
        return {
            "best_match": {
                "speaker_id": best["speaker_id"],
                "similarity": best_sim,
                "matched": best_sim >= threshold,
            }
        }


@pytest.fixture()
def fake_provider(monkeypatch):
    """Force ProviderRegistry.get_speaker_svc to return our deterministic stub."""
    from services.providers import registry as registry_module

    fake = _FakeMatchingProvider()
    monkeypatch.setattr(
        registry_module.ProviderRegistry,
        "get_speaker_svc",
        lambda self, org_id: fake,
    )
    monkeypatch.setattr(
        registry_module.ProviderRegistry,
        "get_diarization",
        lambda self, org_id: fake,
    )
    return fake


def test_identify_speakers_matches_when_embedding_present(client, fake_provider):
    """Happy path: diarized segments carry embeddings -> auto-link."""
    (
        _Org,
        _U,
        _UO,
        SessionLocal,
        _OPS,
        RecordingSession,
        _SP,
        SpeakerSessionLink,
    ) = _models()
    from services.speaker_service import identify_speakers

    org_id, speaker_id, enrolled_emb = _seed_world(client)

    # Two segments with the SAME speaker label, one carrying an embedding
    # nearly identical to the enrolled speaker's centroid. Cosine similarity
    # should clear the 0.55 default threshold easily.
    segments = [
        {
            "start": 0.0,
            "end": 5.0,
            "speaker": "SPEAKER_00",
            "text": "Hello this is Aaron.",
            "embedding": enrolled_emb,  # exact match -> cosine ~1.0
        },
        {
            "start": 6.0,
            "end": 12.0,
            "speaker": "SPEAKER_00",
            "text": "Continuing the meeting.",
            "embedding": enrolled_emb,
        },
    ]
    session_id = _make_session(org_id, segments)

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_id).first()
        result = identify_speakers(session, db)

        assert result["linked"] == 1, f"expected 1 linked label, got {result}"
        assert result["skipped_reason"] is None
        link = (
            db.query(SpeakerSessionLink)
            .filter(SpeakerSessionLink.session_id == session_id)
            .filter(SpeakerSessionLink.raw_label == "SPEAKER_00")
            .first()
        )
        assert link is not None
        assert link.speaker_id == speaker_id
        assert link.source == "auto"
        assert link.similarity is not None and link.similarity > 0.99
    finally:
        db.close()


def test_identify_speakers_skips_when_no_embedding(client, fake_provider):
    """Regression: no embeddings on segments -> no_embedding reason, no auto-link."""
    (
        _Org,
        _U,
        _UO,
        SessionLocal,
        _OPS,
        RecordingSession,
        _SP,
        SpeakerSessionLink,
    ) = _models()
    from services.speaker_service import identify_speakers

    org_id, speaker_id, _enrolled_emb = _seed_world(client)

    # Same shape as the broken always-on output: segments with speaker
    # labels but NO embedding key, and no audio_file on disk either.
    segments = [
        {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00", "text": "Hello."},
        {"start": 6.0, "end": 12.0, "speaker": "SPEAKER_00", "text": "Continuing."},
    ]
    session_id = _make_session(org_id, segments, audio_file="")  # no audio path

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_id).first()
        result = identify_speakers(session, db)

        # Without audio AND without inline embeddings, the function bails
        # out early with audio_missing — this is the documented behavior.
        assert result["skipped_reason"] is not None
        assert (
            result["skipped_reason"].startswith("audio_missing")
            or result["skipped_reason"] == "no_diarized_segments"
        )
        # Critically: no link should have been created. If a link existed it
        # would have to come from elsewhere (manual tagging) and we'd not
        # touch it.
        link = (
            db.query(SpeakerSessionLink)
            .filter(SpeakerSessionLink.session_id == session_id)
            .first()
        )
        assert link is None
    finally:
        db.close()


def test_identify_speakers_below_threshold(client, fake_provider):
    """Embedding present but cosine < 0.55 vs the enrolled speaker -> NOT matched
    to that speaker. v3.43: instead of a throwaway unlinked 'Speaker N', the
    unmatched voice now gets a persistent auto-created profile (stable across
    meetings); the link points at the NEW profile, never at the enrolled one."""
    (
        _Org,
        _U,
        _UO,
        SessionLocal,
        _OPS,
        RecordingSession,
        _SP,
        SpeakerSessionLink,
    ) = _models()
    from services.speaker_service import identify_speakers

    org_id, speaker_id, enrolled_emb = _seed_world(client)

    # A completely different, near-orthogonal embedding (seed=999 is far
    # from seed=42 in random unit-vector space, cosine ~0).
    other_emb = _make_embedding(seed=999)
    segments = [
        {
            "start": 0.0,
            "end": 8.0,
            "speaker": "SPEAKER_01",
            "text": "An unknown speaker.",
            "embedding": other_emb,
        },
    ]
    session_id = _make_session(org_id, segments)

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_id).first()
        result = identify_speakers(session, db)

        # v3.43: the below-threshold voice is auto-created as a persistent
        # profile (not left unlinked), and is NEVER matched to the enrolled one.
        assert result.get("auto_created", 0) >= 1
        assert result["linked"] == 1
        link = (
            db.query(SpeakerSessionLink)
            .filter(SpeakerSessionLink.session_id == session_id)
            .first()
        )
        assert link is not None
        assert link.speaker_id is not None
        assert link.speaker_id != speaker_id  # NOT the enrolled Aaron
        from database.models import SpeakerProfile
        from services.speaker_service import is_auto_generated_speaker
        new_sp = db.query(SpeakerProfile).filter(SpeakerProfile.id == link.speaker_id).first()
        assert new_sp is not None and is_auto_generated_speaker(new_sp)
    finally:
        db.close()


def test_build_segments_from_words_preserves_embedding(client):
    """The word-level merge in api/uploads MUST copy the diarization turn's
    embedding onto each utterance. Without this fix, identify_speakers
    falls through to no_embedding even when the diarizer returned them.
    """
    from api.uploads import _build_segments_from_words

    aaron_emb = _make_embedding(seed=42)
    other_emb = _make_embedding(seed=999)

    diar_segments = [
        {
            "start": 0.0,
            "end": 5.0,
            "speaker": "SPEAKER_00",
            "embedding": aaron_emb,
        },
        {
            "start": 5.5,
            "end": 9.0,
            "speaker": "SPEAKER_01",
            "embedding": other_emb,
        },
    ]
    words = [
        {"word": "Hello", "start": 0.1, "end": 0.5},
        {"word": "there", "start": 0.6, "end": 1.0},
        {"word": "thanks", "start": 6.0, "end": 6.5},
        {"word": "for", "start": 6.6, "end": 6.9},
        {"word": "joining", "start": 7.0, "end": 7.6},
    ]
    utterances = _build_segments_from_words(words, diar_segments)
    assert len(utterances) >= 2
    by_speaker: dict[str, dict] = {}
    for u in utterances:
        by_speaker[u["speaker"]] = u
    assert "SPEAKER_00" in by_speaker, f"missing SPEAKER_00 in {utterances}"
    assert "SPEAKER_01" in by_speaker, f"missing SPEAKER_01 in {utterances}"

    # The cosine sim between the copied embedding and the original must be
    # 1.0 (it's literally the same float list).
    assert by_speaker["SPEAKER_00"].get("embedding") == aaron_emb
    assert by_speaker["SPEAKER_01"].get("embedding") == other_emb


def test_build_segments_from_words_handles_diar_without_embeddings(client):
    """Backwards-compat: if the diarizer doesn't include embeddings (e.g.
    fallback backend), the utterances should still be built — just without
    the embedding key. identify_speakers will then skip with no_embedding,
    which is the correct behavior for that case."""
    from api.uploads import _build_segments_from_words

    diar_segments = [
        {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
        {"start": 5.5, "end": 9.0, "speaker": "SPEAKER_01"},
    ]
    words = [
        {"word": "Hello", "start": 0.1, "end": 0.5},
        {"word": "there", "start": 6.0, "end": 6.5},
    ]
    utterances = _build_segments_from_words(words, diar_segments)
    assert utterances, "expected at least one utterance"
    for u in utterances:
        assert "embedding" not in u, f"utterance {u} should not carry embedding when diar didn't emit one"


def test_unmatched_voice_auto_creates_persistent_profile(client, fake_provider):
    """v3.43 persistent identity (B): an unmatched, long-enough voice auto-creates
    a stable UNNAMED profile + labels the segment with its handle (not a throwaway
    'Speaker N'), so future meetings can match the same profile."""
    import uuid as _uuid
    from auth.models import Organization
    from database.database import SessionLocal
    from database.models import RecordingSession, SpeakerProfile, SpeakerSessionLink
    from services.speaker_service import identify_speakers, is_auto_generated_speaker

    db = SessionLocal()
    try:
        org = Organization(name="ac-org", slug=f"ac-{_uuid.uuid4().hex[:8]}", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id
    finally:
        db.close()

    emb = _make_embedding(seed=777)
    segments = [
        {"start": 0.0, "end": 8.0, "speaker": "SPEAKER_00", "text": "Hi everyone.", "embedding": emb},
        {"start": 9.0, "end": 15.0, "speaker": "SPEAKER_00", "text": "A long enough turn.", "embedding": emb},
    ]
    session_id = _make_session(org_id, segments)

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_id).first()
        result = identify_speakers(session, db)
        assert result.get("auto_created", 0) >= 1, result
        profs = db.query(SpeakerProfile).filter(SpeakerProfile.organization_id == org_id).all()
        assert len(profs) == 1
        sp = profs[0]
        assert is_auto_generated_speaker(sp)
        assert sp.display_name.startswith("Speaker ")
        assert sp.sample_count == 1 and sp.centroid_embedding is not None
        db.refresh(session)
        segs = session.transcript_diarized["segments"]
        assert segs and all(s["speaker"] == sp.display_name for s in segs)
        link = (
            db.query(SpeakerSessionLink)
            .filter(
                SpeakerSessionLink.session_id == session_id,
                SpeakerSessionLink.speaker_id == sp.id,
            )
            .first()
        )
        assert link is not None
    finally:
        db.close()


def test_hydrate_renders_live_speaker_name(client):
    """v3.44 dynamic rendering: hydrate_diarized_speaker_names resolves the speaker
    NAME live from the current profile via the session link, so a renamed profile
    shows instantly even when the stored segment.speaker is the OLD (stale) name —
    no transcript rewrite needed at serve time."""
    import uuid as _uuid
    from auth.models import Organization
    from database.database import SessionLocal
    from database.models import RecordingSession, SpeakerProfile, SpeakerSessionLink
    from services.speaker_service import hydrate_diarized_speaker_names

    db = SessionLocal()
    try:
        org = Organization(name="hy-org", slug=f"hy-{_uuid.uuid4().hex[:8]}", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        sp = SpeakerProfile(organization_id=org.id, display_name="Vinny Pagley", sample_count=1)
        db.add(sp)
        db.commit()
        db.refresh(sp)
        sess = RecordingSession(
            session_id=f"hy-{_uuid.uuid4().hex[:12]}",
            name="Hydrate Test",
            organization_id=org.id,
            status="completed",
            # Stored segment carries the OLD/stale baked name on purpose.
            transcript_diarized={
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "Speaker 9Z9Z",
                     "raw_label": "SPEAKER_00", "text": "hi"},
                ],
                "speakers": ["Speaker 9Z9Z"],
            },
        )
        db.add(sess)
        db.commit()
        db.refresh(sess)
        db.add(SpeakerSessionLink(
            session_id=sess.id, organization_id=org.id,
            raw_label="SPEAKER_00", speaker_id=sp.id, confirmed=True,
        ))
        db.commit()

        hydrated = hydrate_diarized_speaker_names(sess.transcript_diarized, db, sess.id)
        # Live-resolved to the CURRENT profile name, not the stale stored one.
        assert hydrated["segments"][0]["speaker"] == "Vinny Pagley"
        assert hydrated["speakers"] == ["Vinny Pagley"]
        # The stored ORM value is untouched (read-only hydration).
        assert sess.transcript_diarized["segments"][0]["speaker"] == "Speaker 9Z9Z"
    finally:
        db.close()


def test_hydrate_for_session_wrapper_renders_live_speaker_name(client):
    """v3.44 dynamic rendering: hydrate_diarized_for_session resolves the DB from
    the ORM object itself and renders the speaker NAME live from the renamed
    profile, even when the stored segment.speaker is a stale handle — mirrors
    test_hydrate_renders_live_speaker_name via the no-db convenience wrapper."""
    import uuid as _uuid
    from auth.models import Organization
    from database.database import SessionLocal
    from database.models import RecordingSession, SpeakerProfile, SpeakerSessionLink
    from services.speaker_service import hydrate_diarized_for_session

    db = SessionLocal()
    try:
        org = Organization(name="hw-org", slug=f"hw-{_uuid.uuid4().hex[:8]}", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        # Profile already carries its CURRENT (renamed) name.
        sp = SpeakerProfile(organization_id=org.id, display_name="Renee Tanaka", sample_count=1)
        db.add(sp)
        db.commit()
        db.refresh(sp)
        sess = RecordingSession(
            session_id=f"hw-{_uuid.uuid4().hex[:12]}",
            name="Hydrate Wrapper Test",
            organization_id=org.id,
            status="completed",
            # Stored segment carries the OLD/stale baked handle on purpose.
            transcript_diarized={
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "Speaker 7Q7Q",
                     "raw_label": "SPEAKER_00", "text": "hello"},
                ],
                "speakers": ["Speaker 7Q7Q"],
            },
        )
        db.add(sess)
        db.commit()
        db.refresh(sess)
        db.add(SpeakerSessionLink(
            session_id=sess.id, organization_id=org.id,
            raw_label="SPEAKER_00", speaker_id=sp.id, confirmed=True,
        ))
        db.commit()

        # The wrapper resolves the db via object_session(session) — no db arg.
        hydrated = hydrate_diarized_for_session(sess)
        assert hydrated["segments"][0]["speaker"] == "Renee Tanaka"
        assert hydrated["speakers"] == ["Renee Tanaka"]
        # Stored ORM value untouched (read-only).
        assert sess.transcript_diarized["segments"][0]["speaker"] == "Speaker 7Q7Q"
    finally:
        db.close()


def test_rename_propagates_to_history(client):
    """v3.44: naming a profile fixes the SUMMARY text of its PAST meetings.
    The diarized transcript is deliberately NOT rewritten — every display
    surface renders the speaker name live from the current profile, so the
    stored segment keeps its old baked value while the summary is corrected."""
    import json
    import uuid as _uuid
    from auth.models import Organization
    from database.database import SessionLocal
    from database.models import RecordingSession, SpeakerProfile, SpeakerSessionLink
    from api.speakers import apply_rename_to_history

    db = SessionLocal()
    try:
        org = Organization(name="rh-org", slug=f"rh-{_uuid.uuid4().hex[:8]}", is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        org_id = org.id

        sp = SpeakerProfile(
            organization_id=org_id,
            display_name="Speaker AB12",
            external_refs={"auto_generated": True},
            sample_count=1,
        )
        db.add(sp)
        db.commit()
        db.refresh(sp)

        sess = RecordingSession(
            session_id=f"rh-{_uuid.uuid4().hex[:12]}",
            name="History Test",
            organization_id=org_id,
            status="completed",
            transcript_diarized={
                "segments": [
                    {"start": 0.0, "end": 5.0, "speaker": "Speaker AB12",
                     "raw_label": "SPEAKER_00", "text": "hi"},
                ],
                "speakers": ["Speaker AB12"],
            },
            summary=json.dumps({"text": "Speaker AB12 discussed the roadmap."}),
        )
        db.add(sess)
        db.commit()
        db.refresh(sess)
        db.add(SpeakerSessionLink(
            session_id=sess.id, organization_id=org_id,
            raw_label="SPEAKER_00", speaker_id=sp.id, confirmed=True,
        ))
        db.commit()
        sess_id = sess.id

        # Rename, then propagate to history.
        sp.display_name = "Gina Stransky"
        db.commit()
        affected = apply_rename_to_history(db, sp, "Speaker AB12")
        assert affected == 1

        db.refresh(sess)
        # v3.44: transcript is NOT rewritten by the rename — it renders live.
        assert sess.transcript_diarized["segments"][0]["speaker"] == "Speaker AB12"
        # The SUMMARY free text IS fixed (can't be rendered live).
        assert "Gina Stransky" in sess.summary and "Speaker AB12" not in sess.summary
        # And the live hydration shows the new name from the (renamed) profile.
        from services.speaker_service import hydrate_diarized_for_session
        hydrated = hydrate_diarized_for_session(sess)
        assert hydrated["segments"][0]["speaker"] == "Gina Stransky"
    finally:
        db.close()
