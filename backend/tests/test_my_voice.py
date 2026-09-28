""""My Voice" portable account-level voiceprint tests (v3.36.0).

Covers:
  (a) a member's my-voice candidate is matched LOCALLY by identify_speakers
      when the org library is empty -> link created + org SpeakerProfile
      bootstrapped with linked_user_id;
  (b) a NON-member's my-voice profile is never considered (tenant isolation);
  (c) is_me naming (PATCH speaker-link) folds the pooled session embedding
      into user_voice_profile + sets speaker.linked_user_id;
  (d) is_me on a speaker linked to ANOTHER user -> 409;
  (e) GET / DELETE /api/me/voice lifecycle (incl. enroll-from-session);
  (f) is_me on the rename+enroll path claims + folds;
  (g) consistency floor: mismatched/dim-mismatched enroll -> 409, no fold;
  (h) email match alone never folds (no implicit biometric consent);
  (i) DELETE purges my-voice-seeded org centroids, keeps sample-backed ones;
  (j) a bad (dim-mismatched) my-voice candidate never aborts identify;
  (k) name collision bootstraps a "(2)" profile instead of stealing a
      linked one;
  (l) a user-CONFIRMED label gets zero my-voice bootstrap side effects;
  (m) v3.36.1 fold-status surfacing: the naming responses carry
      my_voice_folded=true on a successful is_me fold, false on a
      floor-skipped fold, and null when no fold was applicable.

Follows the seed/monkeypatch style of test_summary_idempotency.py /
test_speakers_unassigned_and_merge.py / test_identify_with_embeddings.py:
auth.models is imported before database.models so the users FK resolves.
"""
from __future__ import annotations

import uuid

import numpy as np
import pytest

from auth.utils import get_password_hash


def _models():
    from auth.models import Organization, User, UserOrganization
    from database.database import SessionLocal
    from database.models import (
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
        UserVoiceProfile,
    )

    return (
        Organization,
        User,
        UserOrganization,
        SessionLocal,
        RecordingSession,
        SpeakerProfile,
        SpeakerSessionLink,
        UserVoiceProfile,
    )


def _make_embedding(seed: int, dim: int = 256) -> list[float]:
    rng = np.random.default_rng(seed)
    vec = rng.normal(size=dim).astype("float32")
    vec = vec / np.linalg.norm(vec)
    return vec.tolist()


def _seed_user_org(*, slug: str, username: str, role: str = "user", full_name: str | None = None):
    Organization, User, UserOrganization, SessionLocal, *_ = _models()
    db = SessionLocal()
    try:
        org = db.query(Organization).filter(Organization.slug == slug).first()
        if not org:
            org = Organization(name=slug.replace("-", " ").title(), slug=slug, is_active=True, plan="pro")  # billing-1: paid workspace matches the pro user
            db.add(org)
            db.commit()
            db.refresh(org)

        user = db.query(User).filter(User.username == username).first()
        if not user:
            user = User(
                email=f"{username}@meeting-ops.local",
                username=username,
                full_name=full_name,
                hashed_password=get_password_hash("admin123"),
                is_active=True,
                is_verified=True,
                tier="pro",
            )
            db.add(user)
            db.commit()
            db.refresh(user)

        membership = (
            db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == user.id,
                UserOrganization.organization_id == org.id,
            )
            .first()
        )
        if not membership:
            db.add(UserOrganization(user_id=user.id, organization_id=org.id, role=role))
            db.commit()
        db.refresh(org)
        db.refresh(user)
        return org, user
    finally:
        db.close()


def _seed_my_voice(user_id: int, embedding: list[float]):
    *_, UserVoiceProfile = _models()
    from database.database import SessionLocal
    from services.speaker_service import encode_embedding

    db = SessionLocal()
    try:
        profile = db.query(UserVoiceProfile).filter(UserVoiceProfile.user_id == user_id).first()
        if profile is None:
            profile = UserVoiceProfile(user_id=user_id)
            db.add(profile)
        profile.centroid_embedding = encode_embedding(embedding)
        profile.embedding_dim = len(embedding)
        profile.embedding_model = "test-model"
        profile.sample_count = 1
        db.commit()
    finally:
        db.close()


def _seed_session(*, org_id: int, user_id: int, label: str = "SPEAKER_00",
                  embedding: list[float] | None = None, with_turns: bool = False):
    _, _, _, SessionLocal, RecordingSession, *_ = _models()
    db = SessionLocal()
    try:
        diarized: dict = {
            "segments": [
                {
                    "idx": 0,
                    "speaker": label,
                    "raw_label": label,
                    "start": 0.0,
                    "end": 10.0,
                    "text": "Hello from the cluster under test.",
                    **({"embedding": embedding} if embedding else {}),
                },
            ],
            "speakers": [label],
        }
        if with_turns and embedding:
            diarized["speaker_turns"] = [
                {"speaker": label, "start": 0.0, "end": 10.0, "embedding": embedding},
                {"speaker": label, "start": 12.0, "end": 20.0, "embedding": embedding},
            ]
        session = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="My Voice Test",
            title="My Voice Test",
            status="completed",
            user_id=user_id,
            organization_id=org_id,
            transcript_diarized=diarized,
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        return session.id
    finally:
        db.close()


def _login_headers(client, username: str, org_slug: str):
    resp = client.post("/api/auth/login", data={"username": username, "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {
        "Authorization": f"Bearer {resp.json()['access_token']}",
        "X-MeetingOps-Org": org_slug,
    }


class _FakeMatchingProvider:
    """Deterministic stand-in for the speaker-svc provider (cosine identify)."""

    async def identify(self, embedding, candidates, threshold):
        q = np.asarray(embedding, dtype="float32")
        best, best_sim = None, -1.0
        for c in candidates:
            e = np.asarray(c["embedding"], dtype="float32")
            denom = np.linalg.norm(q) * np.linalg.norm(e)
            sim = float(np.dot(q, e) / denom) if denom > 0 else 0.0
            if sim > best_sim:
                best_sim, best = sim, c
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
    from services.providers import registry as registry_module

    fake = _FakeMatchingProvider()
    monkeypatch.setattr(
        registry_module.ProviderRegistry,
        "get_speaker_svc",
        lambda self, org_id: fake,
    )
    return fake


# ---------- (a) my-voice candidate matched locally, org library empty ----------


def test_my_voice_match_bootstraps_org_speaker(client, fake_provider):
    _, _, _, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink, _ = _models()
    from services.speaker_service import identify_speakers

    org, user = _seed_user_org(
        slug="myvoice-org-a", username="myvoice_user_a", full_name="Mira Voiceprint"
    )
    emb = _make_embedding(seed=7)
    _seed_my_voice(user.id, emb)
    session_pk = _seed_session(org_id=org.id, user_id=user.id, embedding=emb)

    db = SessionLocal()
    try:
        # Org speaker library is EMPTY — only the my-voice candidate exists.
        assert (
            db.query(SpeakerProfile)
            .filter(SpeakerProfile.organization_id == org.id)
            .count()
            == 0
        )
        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        result = identify_speakers(session, db)
        assert result["linked"] == 1, f"expected my-voice link, got {result}"

        link = (
            db.query(SpeakerSessionLink)
            .filter(SpeakerSessionLink.session_id == session_pk)
            .first()
        )
        assert link is not None
        assert link.speaker_id is not None
        assert link.source == "auto"
        assert link.similarity is not None and link.similarity > 0.99

        speaker = db.query(SpeakerProfile).filter(SpeakerProfile.id == link.speaker_id).first()
        assert speaker is not None
        assert speaker.organization_id == org.id
        assert speaker.linked_user_id == user.id
        assert speaker.display_name == "Mira Voiceprint"
        assert speaker.email == user.email
        # Centroid seeded from the account voiceprint.
        assert speaker.centroid_embedding is not None
        assert speaker.sample_count == 1

        # The diarized transcript got rewritten to the display name.
        seg = session.transcript_diarized["segments"][0]
        assert seg["speaker"] == "Mira Voiceprint"
        assert seg["raw_label"] == "SPEAKER_00"
    finally:
        db.close()


# ---------- (b) non-member my-voice never considered ----------


def test_non_member_my_voice_is_isolated(client, fake_provider):
    _, _, _, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink, _ = _models()
    from services.speaker_service import identify_speakers

    org_a, member = _seed_user_org(slug="myvoice-iso-a", username="myvoice_iso_member")
    org_b, outsider = _seed_user_org(
        slug="myvoice-iso-b", username="myvoice_iso_outsider", full_name="Otto Outsider"
    )
    emb = _make_embedding(seed=11)
    # The OUTSIDER (member of org B only) has a my-voice profile that matches
    # the session audio perfectly — it must never be considered in org A.
    _seed_my_voice(outsider.id, emb)
    session_pk = _seed_session(org_id=org_a.id, user_id=member.id, embedding=emb)

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        result = identify_speakers(session, db)

        # v3.43: the unmatched voice auto-creates a FRESH org-A profile (stable
        # identity for that voice), but the OUTSIDER's my-voice never leaks in —
        # the new profile is an anonymous handle, not "Otto Outsider", and is not
        # claimed by that user. Cross-tenant isolation is preserved.
        from services.speaker_service import is_auto_generated_speaker
        assert result.get("auto_created", 0) >= 1, f"expected auto-create: {result}"
        profs = (
            db.query(SpeakerProfile)
            .filter(SpeakerProfile.organization_id == org_a.id)
            .all()
        )
        assert len(profs) == 1
        sp = profs[0]
        assert is_auto_generated_speaker(sp)
        assert sp.display_name != "Otto Outsider"  # no cross-tenant identity leak
        assert sp.linked_user_id is None  # not claimed by the outsider
        link = (
            db.query(SpeakerSessionLink)
            .filter(SpeakerSessionLink.session_id == session_pk)
            .first()
        )
        assert link is not None and link.speaker_id == sp.id
    finally:
        db.close()


# ---------- (c) is_me naming folds into user_voice_profile ----------


def test_is_me_assign_folds_into_user_voice(client):
    _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink, UserVoiceProfile = _models()

    org, user = _seed_user_org(slug="myvoice-isme", username="myvoice_isme_user")
    emb = _make_embedding(seed=21)
    session_pk = _seed_session(
        org_id=org.id, user_id=user.id, embedding=emb, with_turns=True
    )

    db = SessionLocal()
    try:
        speaker = SpeakerProfile(organization_id=org.id, display_name="Me Myself")
        db.add(speaker)
        db.flush()
        link = SpeakerSessionLink(
            session_id=session_pk,
            organization_id=org.id,
            raw_label="SPEAKER_00",
        )
        db.add(link)
        db.commit()
        speaker_id, link_id = speaker.id, link.id
    finally:
        db.close()

    resp = client.patch(
        f"/api/sessions/{session_pk}/speaker-links/{link_id}",
        headers=_login_headers(client, "myvoice_isme_user", org.slug),
        json={"speaker_id": speaker_id, "confirmed": True, "is_me": True},
    )
    assert resp.status_code == 200, resp.text
    # v3.36.1 fold-status surfacing: a successful is_me fold is reported.
    assert resp.json()["my_voice_folded"] is True

    db = SessionLocal()
    try:
        profile = db.query(UserVoiceProfile).filter(UserVoiceProfile.user_id == user.id).first()
        assert profile is not None, "is_me assign must create user_voice_profile"
        assert profile.centroid_embedding is not None
        assert profile.sample_count == 1
        assert profile.consent_at is not None  # biometric consent stamped on create

        speaker = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker_id).first()
        assert speaker.linked_user_id == user.id
        assert speaker.email == user.email  # backfilled from the caller
    finally:
        db.close()


# ---------- (d) is_me on someone else's speaker -> 409 ----------


def test_is_me_conflicts_with_other_users_speaker(client):
    _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink, UserVoiceProfile = _models()

    org, user = _seed_user_org(slug="myvoice-conflict", username="myvoice_conflict_user")
    _, other = _seed_user_org(slug="myvoice-conflict", username="myvoice_conflict_other")
    emb = _make_embedding(seed=31)
    session_pk = _seed_session(
        org_id=org.id, user_id=user.id, embedding=emb, with_turns=True
    )

    db = SessionLocal()
    try:
        speaker = SpeakerProfile(
            organization_id=org.id,
            display_name="Already Claimed",
            linked_user_id=other.id,
        )
        db.add(speaker)
        db.flush()
        link = SpeakerSessionLink(
            session_id=session_pk,
            organization_id=org.id,
            raw_label="SPEAKER_00",
        )
        db.add(link)
        db.commit()
        speaker_id, link_id = speaker.id, link.id
    finally:
        db.close()

    resp = client.patch(
        f"/api/sessions/{session_pk}/speaker-links/{link_id}",
        headers=_login_headers(client, "myvoice_conflict_user", org.slug),
        json={"speaker_id": speaker_id, "confirmed": True, "is_me": True},
    )
    assert resp.status_code == 409, resp.text

    db = SessionLocal()
    try:
        speaker = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker_id).first()
        assert speaker.linked_user_id == other.id  # untouched
        assert (
            db.query(UserVoiceProfile).filter(UserVoiceProfile.user_id == user.id).first()
            is None
        )
    finally:
        db.close()


# ---------- (e) GET / DELETE /api/me/voice lifecycle ----------


def test_my_voice_lifecycle(client):
    org, user = _seed_user_org(slug="myvoice-life", username="myvoice_life_user")
    headers = _login_headers(client, "myvoice_life_user", org.slug)

    # Fresh account: not enrolled.
    resp = client.get("/api/me/voice", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "enrolled": False,
        "sample_count": 0,
        "embedding_model": None,
        "updated_at": None,
        "consent_at": None,
    }

    # Enroll from a session cluster with a usable turn bank.
    emb = _make_embedding(seed=41)
    session_pk = _seed_session(
        org_id=org.id, user_id=user.id, embedding=emb, with_turns=True
    )
    resp = client.post(
        "/api/me/voice/enroll-from-session",
        headers=headers,
        json={"session_id": session_pk, "raw_label": "SPEAKER_00"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enrolled": True, "sample_count": 1}

    resp = client.get("/api/me/voice", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enrolled"] is True
    assert body["sample_count"] == 1
    assert body["embedding_model"]
    assert body["consent_at"]

    # Unknown label -> 400, no state change.
    resp = client.post(
        "/api/me/voice/enroll-from-session",
        headers=headers,
        json={"session_id": session_pk, "raw_label": "SPEAKER_99"},
    )
    assert resp.status_code == 400

    # Delete is idempotent 204.
    resp = client.delete("/api/me/voice", headers=headers)
    assert resp.status_code == 204
    resp = client.get("/api/me/voice", headers=headers)
    assert resp.json()["enrolled"] is False
    resp = client.delete("/api/me/voice", headers=headers)
    assert resp.status_code == 204


# ---------- (f) is_me on the rename+enroll path (POST .../enroll) ----------
# Integration-pass regression: the frontend "name a new speaker" UX posts
# is_me to POST /speaker-links/{id}/enroll (NOT /create-speaker), so that
# endpoint must claim the profile + fold the fresh sample too.


class _FakeEmbeddingProvider:
    def __init__(self, embedding: list[float]):
        self._embedding = embedding

    async def embed_bytes(self, audio_bytes, filename=""):
        return {
            "embedding": list(self._embedding),
            "model": "test-model",
            "duration_seconds": 5.0,
        }


def test_is_me_on_enroll_endpoint_folds_and_claims(client, monkeypatch, tmp_path):
    _, _, _, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink, UserVoiceProfile = _models()

    org, user = _seed_user_org(slug="myvoice-enroll", username="myvoice_enroll_user")
    emb = _make_embedding(seed=51)
    session_pk = _seed_session(org_id=org.id, user_id=user.id, embedding=emb)

    # Give the session a real (placeholder) audio file and fake out ffmpeg +
    # the speaker-svc embedder so the endpoint runs hermetically.
    audio_file = tmp_path / "meeting.wav"
    audio_file.write_bytes(b"RIFFfake")
    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        session.audio_file = str(audio_file)
        db.commit()
        link = SpeakerSessionLink(
            session_id=session_pk,
            organization_id=org.id,
            raw_label="SPEAKER_00",
        )
        db.add(link)
        db.commit()
        link_id = link.id
    finally:
        db.close()

    import subprocess

    class _FakeProc:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc())

    from services.providers import registry as registry_module

    fake = _FakeEmbeddingProvider(emb)
    monkeypatch.setattr(
        registry_module.ProviderRegistry,
        "get_speaker_svc",
        lambda self, org_id: fake,
    )

    resp = client.post(
        f"/api/sessions/{session_pk}/speaker-links/{link_id}/enroll",
        headers=_login_headers(client, "myvoice_enroll_user", org.slug),
        json={"display_name": "Enrolled Me", "is_me": True},
    )
    assert resp.status_code == 200, resp.text
    # v3.36.1 fold-status surfacing on the enroll path too.
    assert resp.json()["my_voice_folded"] is True

    db = SessionLocal()
    try:
        speaker = (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.organization_id == org.id,
                SpeakerProfile.display_name == "Enrolled Me",
            )
            .first()
        )
        assert speaker is not None
        assert speaker.linked_user_id == user.id  # is_me claim applied
        assert speaker.email == user.email  # backfilled

        profile = db.query(UserVoiceProfile).filter(UserVoiceProfile.user_id == user.id).first()
        assert profile is not None, "is_me enroll must fold into user_voice_profile"
        assert profile.sample_count == 1
        assert profile.consent_at is not None
    finally:
        db.close()


# ---------- (g) consistency floor: inconsistent enroll -> 409, no fold ----------


def test_enroll_consistency_floor_rejects_mismatched_voice(client):
    """Anti-poisoning: once enrolled, a near-orthogonal sample (someone
    ELSE's voice) is refused with 409 and the voiceprint is untouched."""
    org, user = _seed_user_org(slug="myvoice-floor", username="myvoice_floor_user")
    headers = _login_headers(client, "myvoice_floor_user", org.slug)

    emb_mine = _make_embedding(seed=61)
    sess_mine = _seed_session(
        org_id=org.id, user_id=user.id, embedding=emb_mine, with_turns=True
    )
    resp = client.post(
        "/api/me/voice/enroll-from-session",
        headers=headers,
        json={"session_id": sess_mine, "raw_label": "SPEAKER_00"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enrolled": True, "sample_count": 1}

    # Independent random unit vectors in 256-d are ~orthogonal (cos ~ 0).
    emb_other = _make_embedding(seed=62)
    sess_other = _seed_session(
        org_id=org.id, user_id=user.id, embedding=emb_other, with_turns=True
    )
    resp = client.post(
        "/api/me/voice/enroll-from-session",
        headers=headers,
        json={"session_id": sess_other, "raw_label": "SPEAKER_00"},
    )
    assert resp.status_code == 409, resp.text
    assert "doesn't match your enrolled voice fingerprint" in resp.json()["detail"]

    # Voiceprint untouched.
    resp = client.get("/api/me/voice", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["sample_count"] == 1

    # A dim-mismatched sample is the same 409 (no centroid restart).
    emb_192 = _make_embedding(seed=63, dim=192)
    sess_192 = _seed_session(
        org_id=org.id, user_id=user.id, embedding=emb_192, with_turns=True
    )
    resp = client.post(
        "/api/me/voice/enroll-from-session",
        headers=headers,
        json={"session_id": sess_192, "raw_label": "SPEAKER_00"},
    )
    assert resp.status_code == 409, resp.text
    resp = client.get("/api/me/voice", headers=headers)
    assert resp.json()["sample_count"] == 1


def test_org_enroll_floor_and_idempotency(client):
    """ORG SpeakerProfile add_voice_sample: bootstraps on the first sample,
    accepts a consistent refinement, REJECTS an inconsistent (mixed/wrong-
    cluster) sample instead of poisoning the centroid, and is idempotent per
    source_session so a re-confirm doesn't double-weight one cluster."""
    import numpy as np
    from database.database import SessionLocal
    from database.models import SpeakerProfile
    from services.speaker_service import add_voice_sample, cosine_similarity

    org, _user = _seed_user_org(slug="org-enroll-floor", username="org_enroll_floor_user")
    db = SessionLocal()
    try:
        sp = SpeakerProfile(organization_id=org.id, display_name="Org Floor Test")
        db.add(sp)
        db.commit()
        db.refresh(sp)

        base = _make_embedding(seed=71)
        # First sample bootstraps the identity unconditionally.
        s1 = add_voice_sample(db, sp, base, source="session",
                              embedding_model="m", source_session_id=1001)
        assert s1 is not None and sp.sample_count == 1

        # Consistent refinement (base + small noise -> high cosine) is accepted.
        arr = np.asarray(base, dtype="float32")
        noise = np.asarray(_make_embedding(seed=72), dtype="float32") * 0.05
        consistent = (arr + noise)
        consistent = (consistent / np.linalg.norm(consistent)).tolist()
        assert cosine_similarity(consistent, base) > 0.9
        s2 = add_voice_sample(db, sp, consistent, source="assign_confirm",
                              embedding_model="m", source_session_id=1002)
        assert s2 is not None and sp.sample_count == 2

        # Inconsistent (independent ~orthogonal vector = someone else's voice)
        # is REJECTED -> None, centroid/count untouched (no poisoning).
        other = _make_embedding(seed=99)
        s3 = add_voice_sample(db, sp, other, source="assign_confirm",
                              embedding_model="m", source_session_id=1003)
        assert s3 is None
        assert sp.sample_count == 2

        # Idempotent: re-confirming the SAME session returns the existing sample
        # and does not add another (no double-weight in the running mean).
        s4 = add_voice_sample(db, sp, consistent, source="assign_confirm",
                              embedding_model="m", source_session_id=1002)
        assert s4 is not None and s4.id == s2.id
        assert sp.sample_count == 2
    finally:
        db.rollback()
        db.close()


# ---------- (h) email match alone must NOT fold (no implicit biometric consent) ----------


def test_email_match_without_is_me_does_not_fold(client):
    """A speaker whose email happens to equal the caller's is NOT consent:
    a confirmed assign with is_me=False must create no user_voice_profile."""
    _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink, UserVoiceProfile = _models()

    org, user = _seed_user_org(slug="myvoice-email", username="myvoice_email_user")
    emb = _make_embedding(seed=71)
    session_pk = _seed_session(
        org_id=org.id, user_id=user.id, embedding=emb, with_turns=True
    )

    db = SessionLocal()
    try:
        speaker = SpeakerProfile(
            organization_id=org.id,
            display_name="Email Twin",
            email=user.email,  # the (formerly implicit-fold) email match
        )
        db.add(speaker)
        db.flush()
        link = SpeakerSessionLink(
            session_id=session_pk,
            organization_id=org.id,
            raw_label="SPEAKER_00",
        )
        db.add(link)
        db.commit()
        speaker_id, link_id = speaker.id, link.id
    finally:
        db.close()

    resp = client.patch(
        f"/api/sessions/{session_pk}/speaker-links/{link_id}",
        headers=_login_headers(client, "myvoice_email_user", org.slug),
        json={"speaker_id": speaker_id, "confirmed": True, "is_me": False},
    )
    assert resp.status_code == 200, resp.text
    # v3.36.1 fold-status surfacing: no self-claim -> no fold applicable ->
    # null/absent (never false, which would imply an attempt was made).
    assert resp.json().get("my_voice_folded") is None

    db = SessionLocal()
    try:
        assert (
            db.query(UserVoiceProfile).filter(UserVoiceProfile.user_id == user.id).first()
            is None
        ), "email match alone must never trigger biometric enrollment"
        speaker = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker_id).first()
        assert speaker.linked_user_id is None  # no implicit claim either
    finally:
        db.close()


# ---------- (i) DELETE recomputes linked org-profile centroids ----------


def test_delete_my_voice_purges_seeded_centroids(client):
    """DELETE /api/me/voice destroys the account voiceprint AND any org
    centroids seeded from it: a pure my-voice-seeded profile (no samples) is
    nulled; a profile with a real org SpeakerVoiceSample keeps a centroid
    rebuilt strictly from its own samples."""
    _, _, _, SessionLocal, _, SpeakerProfile, _, UserVoiceProfile = _models()
    from database.models import SpeakerVoiceSample
    from services.speaker_service import encode_embedding

    org, user = _seed_user_org(slug="myvoice-del", username="myvoice_del_user")
    account_emb = _make_embedding(seed=81)
    sample_emb = _make_embedding(seed=82)
    _seed_my_voice(user.id, account_emb)

    db = SessionLocal()
    try:
        # Pure my-voice-seeded bootstrap profile: centroid copied from the
        # account voiceprint, NO SpeakerVoiceSample rows.
        seeded = SpeakerProfile(
            organization_id=org.id,
            display_name="Seeded Only",
            linked_user_id=user.id,
            centroid_embedding=encode_embedding(account_emb),
            embedding_dim=len(account_emb),
            embedding_model="test-model",
            sample_count=1,
        )
        db.add(seeded)
        # Linked profile with a REAL org-harvested sample (org-tenant data —
        # it must survive the account-voiceprint deletion).
        sampled = SpeakerProfile(
            organization_id=org.id,
            display_name="Has Samples",
            linked_user_id=user.id,
            centroid_embedding=encode_embedding(account_emb),  # seed + sample mix
            embedding_dim=len(account_emb),
            embedding_model="test-model",
            sample_count=2,
        )
        db.add(sampled)
        db.flush()
        db.add(SpeakerVoiceSample(
            speaker_id=sampled.id,
            organization_id=org.id,
            source="session",
            embedding=encode_embedding(sample_emb),
            embedding_dim=len(sample_emb),
            embedding_model="test-model",
        ))
        db.commit()
        seeded_id, sampled_id = seeded.id, sampled.id
    finally:
        db.close()

    headers = _login_headers(client, "myvoice_del_user", org.slug)
    resp = client.delete("/api/me/voice", headers=headers)
    assert resp.status_code == 204

    db = SessionLocal()
    try:
        assert (
            db.query(UserVoiceProfile).filter(UserVoiceProfile.user_id == user.id).first()
            is None
        )
        seeded = db.query(SpeakerProfile).filter(SpeakerProfile.id == seeded_id).first()
        assert seeded.centroid_embedding is None, "seeded centroid must be purged"
        assert seeded.embedding_dim is None
        assert seeded.embedding_model is None
        assert seeded.sample_count == 0

        sampled = db.query(SpeakerProfile).filter(SpeakerProfile.id == sampled_id).first()
        assert sampled.centroid_embedding is not None, "org samples stay org data"
        assert sampled.sample_count == 1  # the REAL sample count, not the mix
        from services.speaker_service import cosine_similarity, decode_embedding
        rebuilt = decode_embedding(sampled.centroid_embedding, sampled.embedding_dim)
        # Rebuilt strictly from its own sample — not the account centroid.
        assert cosine_similarity(rebuilt, sample_emb) > 0.999
        assert cosine_similarity(rebuilt, account_emb) < 0.5
    finally:
        db.close()


# ---------- (j) bad my-voice candidate must not abort identify ----------


def test_dim_mismatched_my_voice_candidate_does_not_abort_identify(client, fake_provider):
    """One 192-dim my-voice row alongside a valid 256-dim ORG candidate:
    the bad candidate is skipped and the org match still lands."""
    _, _, _, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink, _ = _models()
    from services.speaker_service import encode_embedding, identify_speakers

    org, user = _seed_user_org(slug="myvoice-dim", username="myvoice_dim_user")
    # Account voiceprint from an older 192-d embedding model.
    _seed_my_voice(user.id, _make_embedding(seed=91, dim=192))

    emb = _make_embedding(seed=92)  # 256-d session embedding
    session_pk = _seed_session(org_id=org.id, user_id=user.id, embedding=emb)

    db = SessionLocal()
    try:
        org_speaker = SpeakerProfile(
            organization_id=org.id,
            display_name="Org Match",
            centroid_embedding=encode_embedding(emb),
            embedding_dim=len(emb),
            embedding_model="test-model",
            sample_count=1,
        )
        db.add(org_speaker)
        db.commit()
        org_speaker_id = org_speaker.id

        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        result = identify_speakers(session, db)
        assert result["linked"] == 1, f"org match must land despite bad my-voice row: {result}"

        link = (
            db.query(SpeakerSessionLink)
            .filter(SpeakerSessionLink.session_id == session_pk)
            .first()
        )
        assert link is not None
        assert link.speaker_id == org_speaker_id
    finally:
        db.close()


# ---------- (k) name collision must not steal a linked profile ----------


def test_name_collision_bootstrap_suffixes_instead_of_stealing(client, fake_provider):
    """An org profile with the SAME display_name but linked to a DIFFERENT
    user is someone else's identity: my-voice bootstrap must create a
    suffixed "(2)" profile, never re-link the existing one."""
    _, _, _, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink, _ = _models()
    from services.speaker_service import identify_speakers

    org, user = _seed_user_org(
        slug="myvoice-steal", username="myvoice_steal_user", full_name="Mira Clone"
    )
    _, other = _seed_user_org(slug="myvoice-steal", username="myvoice_steal_other")
    emb = _make_embedding(seed=101)
    _seed_my_voice(user.id, emb)
    session_pk = _seed_session(org_id=org.id, user_id=user.id, embedding=emb)

    db = SessionLocal()
    try:
        existing = SpeakerProfile(
            organization_id=org.id,
            display_name="Mira Clone",
            linked_user_id=other.id,  # already someone ELSE's identity
        )
        db.add(existing)
        db.commit()
        existing_id = existing.id

        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        result = identify_speakers(session, db)
        assert result["linked"] == 1, f"my-voice match expected: {result}"

        existing = db.query(SpeakerProfile).filter(SpeakerProfile.id == existing_id).first()
        assert existing.linked_user_id == other.id, "linked profile must not be stolen"

        link = (
            db.query(SpeakerSessionLink)
            .filter(SpeakerSessionLink.session_id == session_pk)
            .first()
        )
        assert link.speaker_id is not None and link.speaker_id != existing_id
        created = db.query(SpeakerProfile).filter(SpeakerProfile.id == link.speaker_id).first()
        assert created.display_name == "Mira Clone (2)"
        assert created.linked_user_id == user.id
    finally:
        db.close()


# ---------- (l) confirmed label gets zero my-voice side effects ----------


def test_confirmed_link_blocks_my_voice_bootstrap(client, fake_provider):
    """When the user already CONFIRMED who a label is, a my-voice match on
    that label must not bootstrap/claim anything (no phantom profile, link
    untouched)."""
    _, _, _, SessionLocal, RecordingSession, SpeakerProfile, SpeakerSessionLink, _ = _models()
    from services.speaker_service import identify_speakers

    org, user = _seed_user_org(
        slug="myvoice-confirmed", username="myvoice_confirmed_user",
        full_name="Connie Confirmed",
    )
    emb = _make_embedding(seed=111)
    _seed_my_voice(user.id, emb)
    session_pk = _seed_session(org_id=org.id, user_id=user.id, embedding=emb)

    db = SessionLocal()
    try:
        someone_else = SpeakerProfile(
            organization_id=org.id,
            display_name="Someone Else",
        )
        db.add(someone_else)
        db.flush()
        db.add(SpeakerSessionLink(
            session_id=session_pk,
            organization_id=org.id,
            raw_label="SPEAKER_00",
            speaker_id=someone_else.id,
            source="manual",
            confirmed=True,
            confirmed_by_user_id=user.id,
        ))
        db.commit()
        someone_else_id = someone_else.id

        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        identify_speakers(session, db)

        # No phantom profile bootstrapped/claimed for the caller.
        assert (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.organization_id == org.id,
                SpeakerProfile.linked_user_id == user.id,
            )
            .count()
            == 0
        )
        assert (
            db.query(SpeakerProfile)
            .filter(SpeakerProfile.organization_id == org.id)
            .count()
            == 1
        )
        link = (
            db.query(SpeakerSessionLink)
            .filter(SpeakerSessionLink.session_id == session_pk)
            .first()
        )
        assert link.speaker_id == someone_else_id  # confirmed mapping intact
        assert link.confirmed is True
    finally:
        db.close()


# ---------- (m) fold-status surfacing: floor-skipped fold -> false ----------


def test_floor_skipped_fold_reports_false(client):
    """An is_me naming whose pooled embedding fails the consistency floor
    (someone ELSE's voice vs the enrolled voiceprint) still succeeds as a
    naming, but the response reports my_voice_folded=false — the fold was
    attempted and skipped, and the voiceprint is untouched."""
    _, _, _, SessionLocal, _, SpeakerProfile, SpeakerSessionLink, UserVoiceProfile = _models()

    org, user = _seed_user_org(slug="myvoice-foldfalse", username="myvoice_foldfalse_user")
    # Enrolled account voiceprint pointing one way...
    _seed_my_voice(user.id, _make_embedding(seed=121))
    # ...and a session cluster whose embedding is ~orthogonal (random
    # independent 256-d unit vectors), so the consistency floor skips it.
    emb_other = _make_embedding(seed=122)
    session_pk = _seed_session(
        org_id=org.id, user_id=user.id, embedding=emb_other, with_turns=True
    )

    db = SessionLocal()
    try:
        speaker = SpeakerProfile(organization_id=org.id, display_name="Floor Skip Me")
        db.add(speaker)
        db.flush()
        link = SpeakerSessionLink(
            session_id=session_pk,
            organization_id=org.id,
            raw_label="SPEAKER_00",
        )
        db.add(link)
        db.commit()
        speaker_id, link_id = speaker.id, link.id
    finally:
        db.close()

    resp = client.patch(
        f"/api/sessions/{session_pk}/speaker-links/{link_id}",
        headers=_login_headers(client, "myvoice_foldfalse_user", org.slug),
        json={"speaker_id": speaker_id, "confirmed": True, "is_me": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["my_voice_folded"] is False

    db = SessionLocal()
    try:
        # The naming itself landed (claim applied), the voiceprint untouched.
        speaker = db.query(SpeakerProfile).filter(SpeakerProfile.id == speaker_id).first()
        assert speaker.linked_user_id == user.id
        profile = db.query(UserVoiceProfile).filter(UserVoiceProfile.user_id == user.id).first()
        assert profile is not None
        assert profile.sample_count == 1  # the seeded sample only — no fold
    finally:
        db.close()
