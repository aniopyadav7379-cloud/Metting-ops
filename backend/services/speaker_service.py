"""Speaker service — embedding (de)serialization, centroid math, and the
core identify_speakers(session) entry point used by the recording lifecycle.

Embeddings are stored as raw little-endian float32 bytes alongside an
embedding_dim INT, so the same row layout works for ECAPA 192-d today and
ECAPA-XL 256-d / WavLM 768-d tomorrow.

This module is sync (not async) on purpose — it's called from a
BackgroundTasks worker on the request thread, so we use the provider's
sync-friendly httpx call patterns. The provider exposes async methods, so
we run them via asyncio.run when called from sync context.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from rapidfuzz import fuzz as rapidfuzz_fuzz
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from database.models import (
    RecordingSession,
    SpeakerProfile,
    SpeakerSessionLink,
    SpeakerVoiceSample,
)

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_MODEL = os.getenv("SPEAKER_EMBEDDING_MODEL", "pyannote/wespeaker-voxceleb-resnet34-LM")
DEFAULT_IDENTIFY_THRESHOLD = float(os.getenv("SPEAKER_IDENTIFY_THRESHOLD", "0.55"))
# Poisoning guard for ORG SpeakerProfile enrollment (v3.42): once a profile has
# an enrolled centroid, only fold in a sample that is cosine-consistent with it.
# A mislabeled/mixed diarization cluster (e.g. two voices pooled) sits below this
# floor; folding it in drifts the centroid toward the wrong voice and compounds
# mis-IDs. Defaults to the identify threshold — a sample that wouldn't even MATCH
# the profile should not TRAIN it. (Distinct from the lenient account-level
# MY_VOICE_FOLD_CONSISTENCY_FLOOR, which is an is_me-gated single identity.)
SPEAKER_ENROLL_CONSISTENCY_FLOOR = float(
    os.getenv("SPEAKER_ENROLL_CONSISTENCY_FLOOR", str(DEFAULT_IDENTIFY_THRESHOLD))
)
# Persistent speaker identity (v3.43): when a diarized voice matches NO enrolled
# speaker, auto-create a stable UNNAMED profile + enroll its voiceprint instead of
# emitting a throwaway per-session "Speaker N". The same voice then matches the
# same profile in future meetings (same handle), and naming it once propagates
# everywhere. Guarded by a minimum clean-speech duration so noise/crosstalk
# clusters don't spawn junk profiles.
SPEAKER_AUTOCREATE_ENABLED = os.getenv("SPEAKER_AUTOCREATE_ENABLED", "true").lower() in ("1", "true", "yes")
SPEAKER_AUTOCREATE_MIN_SECONDS = float(os.getenv("SPEAKER_AUTOCREATE_MIN_SECONDS", "4.0"))
STORE_SPEAKER_AUDIO = os.getenv("STORE_SPEAKER_AUDIO", "false").lower() in ("1", "true", "yes")
SPEAKER_CONTACT_AUTOSTAMP_THRESHOLD = float(os.getenv("SPEAKER_CONTACT_AUTOSTAMP_THRESHOLD", "0.80"))


# ---------- (de)serialization ----------


def encode_embedding(vec: list[float]) -> bytes:
    """Pack a python list[float] into raw little-endian float32 bytes."""
    return np.asarray(vec, dtype="<f4").tobytes()


def decode_embedding(buf: bytes, dim: Optional[int] = None) -> list[float]:
    """Unpack stored bytes back into list[float]. dim is informational only —
    we always use the byte length to derive the actual count."""
    arr = np.frombuffer(buf, dtype="<f4")
    if dim is not None and arr.shape[0] != dim:
        logger.warning("decode_embedding: stored dim=%d does not match column dim=%d", arr.shape[0], dim)
    return arr.tolist()


def update_centroid(
    old_centroid: Optional[list[float]],
    old_count: int,
    new_embedding: list[float],
) -> list[float]:
    """Online running-mean centroid. L2 normalization keeps cosine sim
    well-behaved for ECAPA."""
    new = np.asarray(new_embedding, dtype="float32")
    if old_centroid is None or old_count <= 0:
        avg = new
    else:
        old = np.asarray(old_centroid, dtype="float32")
        avg = (old * old_count + new) / (old_count + 1)
    norm = np.linalg.norm(avg)
    if norm > 0:
        avg = avg / norm
    return avg.tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity for speaker embeddings."""
    va = np.asarray(a, dtype="float32")
    vb = np.asarray(b, dtype="float32")
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom <= 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def hydrate_diarized_speaker_names(diarized, db: Session, session_id: int):
    """Return a COPY of a diarized doc with each segment's speaker NAME resolved
    LIVE from the current SpeakerProfile via the session's links (raw_label ->
    speaker_id -> display_name). v3.44: this is what lets a rename show instantly
    everywhere without rewriting the stored transcript — the profile-id lives in
    the SpeakerSessionLink, so we render the name at serve time instead of baking
    it. Read-only: no commit, no ORM mutation. Segments with no resolvable link
    keep their stored name (backward-compatible). Cheap: 2 indexed queries +
    in-memory map; safe to call on every serve."""
    if not isinstance(diarized, dict):
        return diarized
    segs = diarized.get("segments")
    if not segs:
        return diarized
    try:
        links = (
            db.query(SpeakerSessionLink)
            .filter(
                SpeakerSessionLink.session_id == session_id,
                SpeakerSessionLink.speaker_id.isnot(None),
            )
            .all()
        )
        if not links:
            return diarized
        spk_ids = {l.speaker_id for l in links}
        names = {
            sp.id: sp.display_name
            for sp in db.query(SpeakerProfile).filter(SpeakerProfile.id.in_(spk_ids)).all()
        }
        label_to_name: dict[str, str] = {}
        for l in links:
            nm = names.get(l.speaker_id)
            if nm:
                label_to_name[(l.raw_label or "").strip()] = nm
        if not label_to_name:
            return diarized
        dirty = False
        new_segs = []
        for seg in segs:
            key = ((seg.get("raw_label") or seg.get("speaker")) or "").strip()
            nm = label_to_name.get(key)
            if nm and seg.get("speaker") != nm:
                new_segs.append({**seg, "speaker": nm})
                dirty = True
            else:
                new_segs.append(seg)
        if not dirty:
            return diarized
        out = dict(diarized)
        out["segments"] = new_segs
        if "speakers" in out:
            seen, ordered = set(), []
            for seg in new_segs:
                s = seg.get("speaker")
                if s and s not in seen:
                    seen.add(s)
                    ordered.append(s)
            out["speakers"] = ordered
        return out
    except Exception:  # noqa: BLE001 - serving must never fail on hydration
        logger.exception("hydrate_diarized_speaker_names failed session=%s", session_id)
        return diarized


def hydrate_diarized_for_session(session):
    """Convenience wrapper: hydrate a session's diarized doc live from the
    current SpeakerProfiles, resolving the DB from the ORM object itself.

    v3.44: lets call sites that only have the ORM ``session`` (no ``db``
    param) render names live without a signature change. Returns the doc
    unchanged when the session is detached (no bound Session) or
    ``transcript_diarized`` isn't a dict. Read-only — same guarantees as
    hydrate_diarized_speaker_names."""
    from sqlalchemy.orm import object_session

    db = object_session(session)
    if db is None or not isinstance(getattr(session, "transcript_diarized", None), dict):
        return session.transcript_diarized
    return hydrate_diarized_speaker_names(session.transcript_diarized, db, session.id)


def sanitize_diarized_for_response(diarized):
    """Return a browser-safe copy of a diarized transcript.

    Speaker embeddings are biometric identifiers used only by the backend
    matching pipeline.  They must never cross the API response boundary.
    ``speaker_turns`` is an internal embedding bank and has no UI consumer,
    while older rows may also carry an ``embedding`` directly on a segment.
    Strip both shapes recursively without mutating the persisted JSON value.
    """
    if not isinstance(diarized, dict):
        return diarized

    def _clean(value):
        if isinstance(value, dict):
            return {
                key: _clean(item)
                for key, item in value.items()
                if key not in {"embedding", "embeddings", "centroid_embedding"}
            }
        if isinstance(value, list):
            return [_clean(item) for item in value]
        return value

    safe = _clean(diarized)
    safe.pop("speaker_turns", None)
    return safe


def hydrate_diarized_for_response(session):
    """Hydrate current speaker names, then remove backend-only biometrics."""
    return sanitize_diarized_for_response(hydrate_diarized_for_session(session))


# ---------- DB helpers ----------


def list_org_speakers(db: Session, organization_id: int) -> list[SpeakerProfile]:
    return (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.organization_id == organization_id)
        .order_by(SpeakerProfile.display_name.asc())
        .all()
    )


def find_speaker_by_name_hint(
    db: Session,
    org_id: int,
    name_hint: str,
    *,
    fuzzy: bool = True,
    confidence_floor: float = 0.85,
) -> Optional[SpeakerProfile]:
    """Find an enrolled speaker whose ``display_name`` matches *name_hint*.

    Tries exact case-insensitive match first.  When *fuzzy* is ``True``
    (default) and no exact match is found, falls back to
    ``rapidfuzz.fuzz.token_set_ratio`` against all enrolled speakers in the
    org, returning the highest-scoring match above *confidence_floor*.

    Cross-org isolation is inherent — the query is scoped to *org_id*.
    """
    if not name_hint:
        return None

    query = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.organization_id == org_id)
    )
    speakers: list[SpeakerProfile] = query.all()
    if not speakers:
        return None

    # Exact case-insensitive match first.
    hint_lower = name_hint.lower().strip()
    for sp in speakers:
        if sp.display_name and sp.display_name.lower().strip() == hint_lower:
            return sp

    if not fuzzy:
        return None

    # Fuzzy match — token_set_ratio is robust to word order and partial
    # matches (e.g. "Khan, Shafen" ↔ "Shafen Khan").
    best: Optional[SpeakerProfile] = None
    best_score = 0.0
    for sp in speakers:
        if not sp.display_name:
            continue
        score = rapidfuzz_fuzz.token_set_ratio(name_hint, sp.display_name) / 100.0
        if score > best_score:
            best_score = score
            best = sp

    if best and best_score >= confidence_floor:
        return best
    return None


def get_speaker(db: Session, organization_id: int, speaker_id: int) -> Optional[SpeakerProfile]:
    return (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.organization_id == organization_id,
            SpeakerProfile.id == speaker_id,
        )
        .first()
    )


def candidates_for_org(db: Session, organization_id: int) -> list[dict]:
    """Build the candidate list expected by speaker-svc /identify — only
    returns speakers that already have at least one enrollment."""
    rows = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.organization_id == organization_id,
            SpeakerProfile.centroid_embedding.isnot(None),
            SpeakerProfile.embedding_dim.isnot(None),
        )
        .all()
    )
    out: list[dict] = []
    for row in rows:
        try:
            out.append({
                "speaker_id": row.id,
                "embedding": decode_embedding(row.centroid_embedding, row.embedding_dim),
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping speaker %d (decode failed): %s", row.id, exc)
    return out


def my_voice_candidates_for_org(db: Session, organization_id: int) -> list[dict]:
    """"My Voice" candidates for identification inside *organization_id*.

    Voiceprints are hard tenant-scoped by design (biometric privacy). The one
    sanctioned exception is the user's OWN account-level voiceprint
    (user_voice_profile), and it may only participate in identification
    inside workspaces where that user is a MEMBER — enforced here by joining
    through user_organizations. These candidates are matched LOCALLY
    (numpy cosine) and never sent through the external provider.identify
    call (its speaker_id plumbing is org SpeakerProfile ids).
    """
    # Lazy imports: auth.models for the membership join (speaker_service
    # otherwise has no auth dependency), database.models for reload-safety
    # under the test fixture.
    from auth.models import User, UserOrganization
    from database.models import UserVoiceProfile

    rows = (
        db.query(UserVoiceProfile, User)
        .join(User, User.id == UserVoiceProfile.user_id)
        .join(UserOrganization, UserOrganization.user_id == UserVoiceProfile.user_id)
        .filter(
            UserOrganization.organization_id == organization_id,
            UserVoiceProfile.centroid_embedding.isnot(None),
        )
        .all()
    )
    out: list[dict] = []
    for profile, user in rows:
        try:
            emb = decode_embedding(profile.centroid_embedding, profile.embedding_dim)
            if not emb:
                logger.warning("Skipping my-voice profile user=%d (empty decoded centroid)", user.id)
                continue
            out.append({
                "user": user,
                "user_id": user.id,
                "embedding": emb,
                "embedding_model": profile.embedding_model,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("Skipping my-voice profile user=%d (decode failed): %s", user.id, exc)
    return out


# Voiceprint-poisoning floor (v3.36.0 hardening): once a voiceprint exists,
# a new fold must be cosine-consistent with the enrolled centroid or it is
# SKIPPED. Cross-speaker cosine on these embeddings sits near 0; genuine
# same-speaker pooled samples sit well above 0.30 even across acoustic
# conditions (phone-band vs desk mic).
MY_VOICE_FOLD_CONSISTENCY_FLOOR = float(
    os.getenv("MY_VOICE_FOLD_CONSISTENCY_FLOOR", "0.30")
)


def fold_embedding_into_user_voice(
    db: Session,
    user_id: int,
    embedding: list[float],
    *,
    embedding_model: Optional[str] = None,
):
    """Fold one pooled embedding into the user's account-level "My Voice"
    profile (create the row if missing — stamping consent_at, the biometric
    consent record; consent_at is set only via explicit user action, since
    every caller of this function is gated on an explicit is_me claim or
    the explicit enroll endpoint). Running-centroid via update_centroid +
    sample_count. Caller is responsible for db.commit().

    Poisoning/theft mitigation (v3.36.0 hardening): when the profile already
    has an enrolled centroid (sample_count >= 1), the new embedding is folded
    ONLY when it is dimensionally compatible AND cosine-consistent
    (>= MY_VOICE_FOLD_CONSISTENCY_FLOOR) with that centroid. Inconsistent or
    dim-mismatched samples are SKIPPED — never averaged in, and never allowed
    to restart the centroid (a restart-on-dim-change would be a voiceprint
    replacement primitive). Re-enrolling after an embedding-model upgrade
    requires an explicit DELETE /api/me/voice first.

    Returns ``(profile, status)`` where status is ``"folded"`` or one of
    ``"skipped:dim_mismatch"`` / ``"skipped:low_similarity"`` so callers can
    react (the explicit enroll endpoint surfaces skips as HTTP 409; the
    best-effort speaker-naming folds just log).
    """
    from database.models import UserVoiceProfile

    if not embedding:
        raise ValueError("empty embedding")

    profile = (
        db.query(UserVoiceProfile)
        .filter(UserVoiceProfile.user_id == user_id)
        .first()
    )
    if profile is None:
        profile = UserVoiceProfile(
            user_id=user_id,
            sample_count=0,
            consent_at=datetime.now(timezone.utc),
        )
        db.add(profile)
        db.flush()

    old_centroid = None
    old_count = profile.sample_count or 0
    if profile.centroid_embedding and old_count >= 1:
        if profile.embedding_dim not in (None, len(embedding)):
            logger.warning(
                "my-voice fold SKIPPED: dim mismatch %s -> %s for user=%s "
                "(clear the voiceprint to re-enroll)",
                profile.embedding_dim, len(embedding), user_id,
            )
            return profile, "skipped:dim_mismatch"
        old_centroid = decode_embedding(profile.centroid_embedding, profile.embedding_dim)
        if not old_centroid or len(old_centroid) != len(embedding):
            logger.warning(
                "my-voice fold SKIPPED: stored centroid unusable (len=%s vs %s) for user=%s",
                len(old_centroid or []), len(embedding), user_id,
            )
            return profile, "skipped:dim_mismatch"
        sim = cosine_similarity(embedding, old_centroid)
        if sim < MY_VOICE_FOLD_CONSISTENCY_FLOOR:
            logger.warning(
                "my-voice fold SKIPPED: sample inconsistent with enrolled "
                "voiceprint (cos=%.3f < %.2f) for user=%s",
                sim, MY_VOICE_FOLD_CONSISTENCY_FLOOR, user_id,
            )
            return profile, "skipped:low_similarity"

    new_centroid = update_centroid(old_centroid, old_count if old_centroid else 0, embedding)
    profile.centroid_embedding = encode_embedding(new_centroid)
    profile.embedding_dim = len(new_centroid)
    profile.embedding_model = embedding_model or profile.embedding_model or DEFAULT_EMBEDDING_MODEL
    profile.sample_count = old_count + 1
    if profile.consent_at is None:
        profile.consent_at = datetime.now(timezone.utc)
    db.flush()
    return profile, "folded"


def _bootstrap_speaker_for_user(
    db: Session,
    organization_id: int,
    user,
    mv_candidate: dict,
) -> Optional[SpeakerProfile]:
    """Find-or-create the org SpeakerProfile for a my-voice-matched user.

    Lookup order: linked_user_id, then case-insensitive display_name ==
    user.full_name (claiming it via linked_user_id), then CREATE. The
    (organization_id, display_name) unique index means a same-name profile
    belonging to a DIFFERENT person forces a " (2)" suffix.
    """
    speaker = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.organization_id == organization_id,
            SpeakerProfile.linked_user_id == user.id,
        )
        .first()
    )

    if speaker is None:
        full_name = (user.full_name or "").strip()
        if full_name:
            # Only claim an UNLINKED same-name profile — a profile already
            # linked to a DIFFERENT user is someone else's identity and must
            # fall through to the suffixed-create branch below (v3.36.0
            # hardening: name collision must never steal a linked profile).
            speaker = (
                db.query(SpeakerProfile)
                .filter(
                    SpeakerProfile.organization_id == organization_id,
                    func.lower(SpeakerProfile.display_name) == full_name.lower(),
                    SpeakerProfile.linked_user_id.is_(None),
                )
                .first()
            )
            if speaker is not None:
                speaker.linked_user_id = user.id

    if speaker is None:
        base_name = (user.full_name or user.username or f"User {user.id}").strip()
        name = base_name
        suffix = 2
        # Same display_name in this org but a different person — suffix.
        while (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.organization_id == organization_id,
                func.lower(SpeakerProfile.display_name) == name.lower(),
            )
            .first()
            is not None
        ):
            name = f"{base_name} ({suffix})"
            suffix += 1
        speaker = SpeakerProfile(
            organization_id=organization_id,
            display_name=name,
            email=user.email,
            linked_user_id=user.id,
            created_by_user_id=None,
        )
        db.add(speaker)

    # Seed the org profile's voiceprint from the account centroid so the
    # ORG library can match this person directly on the next meeting.
    if not speaker.centroid_embedding:
        emb = mv_candidate.get("embedding") or []
        if emb:
            speaker.centroid_embedding = encode_embedding(emb)
            speaker.embedding_dim = len(emb)
            speaker.embedding_model = mv_candidate.get("embedding_model") or DEFAULT_EMBEDDING_MODEL
            speaker.sample_count = 1

    db.flush()
    return speaker


def recompute_speaker_from_samples(db: Session, speaker: SpeakerProfile) -> None:
    """Rebuild *speaker*'s centroid strictly from its own SpeakerVoiceSample
    rows (running mean in id order; sample_count = real sample count).

    A profile with NO samples — e.g. a pure my-voice-seeded bootstrap, or one
    whose samples were all deleted — gets centroid_embedding/embedding_dim/
    embedding_model nulled and sample_count=0. Used by speaker merge and by
    DELETE /api/me/voice (which must purge account-voiceprint seeds from
    every linked org profile). Caller is responsible for db.commit().
    """
    samples = (
        db.query(SpeakerVoiceSample)
        .filter(SpeakerVoiceSample.speaker_id == speaker.id)
        .order_by(SpeakerVoiceSample.id.asc())
        .all()
    )
    centroid: Optional[list[float]] = None
    model: Optional[str] = None
    for idx, sample in enumerate(samples):
        vec = decode_embedding(sample.embedding, sample.embedding_dim)
        centroid = update_centroid(centroid, idx, vec)
        model = sample.embedding_model
    if centroid is None:
        speaker.centroid_embedding = None
        speaker.embedding_dim = None
        speaker.embedding_model = None
        speaker.sample_count = 0
    else:
        speaker.centroid_embedding = encode_embedding(centroid)
        speaker.embedding_dim = len(centroid)
        speaker.embedding_model = model
        speaker.sample_count = len(samples)


def add_voice_sample(
    db: Session,
    speaker: SpeakerProfile,
    embedding: list[float],
    *,
    source: str,
    embedding_model: str,
    audio_path: Optional[str] = None,
    duration_seconds: Optional[float] = None,
    source_session_id: Optional[int] = None,
    source_segment_idx: Optional[int] = None,
) -> Optional[SpeakerVoiceSample]:
    """Persist a sample, refresh the centroid, and bump sample_count.

    Returns the SpeakerVoiceSample, or skips and returns:
      * the EXISTING sample — idempotency: a sample from the same
        ``source_session_id`` already exists, so re-confirming a session does
        not double-weight that cluster in the running mean.
      * ``None`` — consistency floor: once the profile has an enrolled centroid
        (sample_count >= 1), a sample whose cosine to the centroid is below
        SPEAKER_ENROLL_CONSISTENCY_FLOOR is NOT folded in. A mislabeled/mixed
        diarization cluster (two voices pooled, or a wrong confirm) sits below
        the floor; averaging it in is exactly what drifts a centroid toward the
        wrong voice and compounds mis-IDs. The FIRST sample (sample_count 0)
        always bootstraps the identity. Mirrors fold_embedding_into_user_voice.

    Callers may ignore the return: the per-session speaker LINK/label is set by
    the caller regardless, so a session is still named correctly even when its
    cluster is too mixed to safely train the fingerprint.
    """
    if not embedding:
        raise ValueError("empty embedding")

    # Idempotency — at most one sample per (speaker, source_session).
    if source_session_id is not None:
        existing = (
            db.query(SpeakerVoiceSample)
            .filter(
                SpeakerVoiceSample.speaker_id == speaker.id,
                SpeakerVoiceSample.source_session_id == source_session_id,
            )
            .first()
        )
        if existing is not None:
            logger.info(
                "add_voice_sample: idempotent skip — speaker=%s already has a sample "
                "from session=%s (no double-weight)", speaker.id, source_session_id,
            )
            return existing

    old_centroid = (
        decode_embedding(speaker.centroid_embedding, speaker.embedding_dim)
        if speaker.centroid_embedding else None
    )
    old_count = speaker.sample_count or 0

    # Consistency floor — refine an established centroid only with a consistent
    # sample; the first sample bootstraps the identity unconditionally.
    if old_centroid is not None and old_count >= 1:
        if speaker.embedding_dim not in (None, len(embedding)) or len(old_centroid) != len(embedding):
            logger.warning(
                "add_voice_sample SKIPPED: dim mismatch %s -> %s for speaker=%s",
                speaker.embedding_dim, len(embedding), speaker.id,
            )
            return None
        sim = cosine_similarity(embedding, old_centroid)
        if sim < SPEAKER_ENROLL_CONSISTENCY_FLOOR:
            logger.warning(
                "add_voice_sample SKIPPED: sample inconsistent with voiceprint "
                "(cos=%.3f < %.2f) speaker=%s source=%s session=%s — not folding a "
                "likely mixed/mislabeled cluster into the centroid",
                sim, SPEAKER_ENROLL_CONSISTENCY_FLOOR, speaker.id, source, source_session_id,
            )
            return None

    sample = SpeakerVoiceSample(
        speaker_id=speaker.id,
        organization_id=speaker.organization_id,
        source=source,
        source_session_id=source_session_id,
        source_segment_idx=source_segment_idx,
        embedding=encode_embedding(embedding),
        embedding_dim=len(embedding),
        embedding_model=embedding_model,
        audio_path=audio_path if STORE_SPEAKER_AUDIO else None,
        duration_seconds=duration_seconds,
    )
    db.add(sample)

    new_centroid = update_centroid(old_centroid, old_count, embedding)
    speaker.centroid_embedding = encode_embedding(new_centroid)
    speaker.embedding_dim = len(new_centroid)
    speaker.embedding_model = embedding_model
    speaker.sample_count = (speaker.sample_count or 0) + 1

    # Annotate sample with cosine sim to the new centroid for diagnostics
    try:
        n = np.asarray(new_centroid, dtype="float32")
        e = np.asarray(embedding, dtype="float32")
        denom = np.linalg.norm(n) * np.linalg.norm(e)
        sample.similarity_to_centroid = float(np.dot(n, e) / denom) if denom > 0 else None
    except Exception:  # noqa: BLE001
        pass

    db.flush()
    return sample


def is_auto_generated_speaker(speaker: SpeakerProfile) -> bool:
    """True for an UNNAMED, auto-created persistent profile (handle like
    'Speaker 3F2A'), flagged in external_refs so the UI can prompt naming and
    so a rename knows to propagate to history."""
    return bool((getattr(speaker, "external_refs", None) or {}).get("auto_generated"))


def _generate_speaker_handle(db: Session, org_id: int) -> str:
    """A stable, human-readable handle for an auto-created profile, e.g.
    'Speaker 3F2A'. Stored as the (org-unique) display_name, so the same voice
    keeps the same handle across meetings until a human names it."""
    import uuid
    for _ in range(6):
        code = uuid.uuid4().hex[:4].upper()
        name = f"Speaker {code}"
        exists = (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.organization_id == org_id,
                SpeakerProfile.display_name == name,
            )
            .first()
        )
        if not exists:
            return name
    return f"Speaker {uuid.uuid4().hex[:8].upper()}"


def auto_create_speaker(
    db: Session,
    org_id: int,
    embedding: list[float],
    *,
    source_session_id: Optional[int] = None,
) -> Optional[SpeakerProfile]:
    """Create a persistent UNNAMED profile for an unmatched voice and enroll its
    voiceprint, so the same voice gets a stable identity across meetings. Returns
    the new profile, or None on failure (best-effort — never blocks identify)."""
    try:
        handle = _generate_speaker_handle(db, org_id)
        sp = SpeakerProfile(
            organization_id=org_id,
            display_name=handle,
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            sample_count=0,
            external_refs={"auto_generated": True},
        )
        db.add(sp)
        db.flush()
        add_voice_sample(
            db, sp, embedding,
            source="auto_identify",
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            source_session_id=source_session_id,
        )
        logger.info(
            "auto-created persistent speaker id=%s (%r) org=%s from an unmatched voice",
            sp.id, handle, org_id,
        )
        return sp
    except Exception:  # noqa: BLE001
        logger.exception("auto_create_speaker failed org=%s", org_id)
        return None


# ---------- the lifecycle hook ----------


def _run_async(coro):
    """Bridge a coroutine into a sync caller.

    Three call sites:
      1. BackgroundTasks worker / Alembic / scripts — no running loop, asyncio.run works
      2. FastAPI request handler — a loop is already running on this thread
         (`asyncio.run` raises RuntimeError). Run the coroutine on a fresh
         loop in a worker thread so we don't try to nest loops.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # We're inside a running event loop — offload to a worker thread.
        import concurrent.futures
        def _runner():
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(_runner).result()


def _segments_from_session(session: RecordingSession) -> list[dict]:
    """Return the list of diarized segments for a session, if any.
    Each segment: {"start": float, "end": float, "speaker": str}."""
    if session.transcript_diarized and isinstance(session.transcript_diarized, dict):
        return session.transcript_diarized.get("segments", []) or []
    return []


def stamp_confirmed_speaker_contacts(
    session: RecordingSession,
    db: Session,
    *,
    threshold: float = SPEAKER_CONTACT_AUTOSTAMP_THRESHOLD,
) -> dict:
    """Best-effort participant stamping from confirmed speaker->contact links.

    A speaker match becomes an asserted meeting participant only when all three
    gates pass: the session link has a speaker_id, the link similarity is at or
    above the high threshold, and the SpeakerProfile has a confirmed
    Contact-Ops contact_id. Everything else is recorded as a suggestion in
    session.processing_metadata["speaker_contact_suggestions"]. Never raises.
    """
    summary = {"stamped": 0, "suggested": 0, "threshold": threshold}
    try:
        links = (
            db.query(SpeakerSessionLink, SpeakerProfile)
            .join(SpeakerProfile, SpeakerProfile.id == SpeakerSessionLink.speaker_id)
            .filter(
                SpeakerSessionLink.session_id == session.id,
                SpeakerSessionLink.organization_id == session.organization_id,
            )
            .all()
        )
        if not links:
            return summary

        participants = list(session.participants) if isinstance(session.participants, list) else []
        existing_contact_ids = {
            str(p.get("contact_id"))
            for p in participants
            if isinstance(p, dict) and p.get("contact_id")
        }
        suggestions: list[dict] = []
        changed = False

        for link, speaker in links:
            contact_id = (speaker.contact_id or "").strip()
            similarity = float(link.similarity or 0.0)
            confirmed_contact = bool(speaker.contact_link_confirmed and contact_id)
            high_confidence = similarity >= threshold

            suggestion = {
                "speaker_id": speaker.id,
                "speaker_name": speaker.display_name,
                "raw_label": link.raw_label,
                "contact_id": contact_id or None,
                "similarity": similarity if link.similarity is not None else None,
                "reason": None,
            }

            if confirmed_contact and high_confidence:
                if contact_id not in existing_contact_ids:
                    participants.append({
                        "id": str(uuid.uuid4()),
                        "name": speaker.display_name,
                        "email": speaker.email or None,
                        "role": "speaker",
                        "contact_id": contact_id,
                    })
                    existing_contact_ids.add(contact_id)
                    summary["stamped"] += 1
                    changed = True
                continue

            if not contact_id:
                suggestion["reason"] = "no_contact_id"
            elif not speaker.contact_link_confirmed:
                suggestion["reason"] = "contact_link_unconfirmed"
            elif not high_confidence:
                suggestion["reason"] = "below_threshold"
            else:
                suggestion["reason"] = "not_stamped"
            suggestions.append(suggestion)

        metadata = dict(session.processing_metadata or {})
        if suggestions:
            metadata["speaker_contact_suggestions"] = suggestions
            metadata["speaker_contact_suggestions_updated_at"] = datetime.now(timezone.utc).isoformat()
            summary["suggested"] = len(suggestions)
            session.processing_metadata = metadata
            flag_modified(session, "processing_metadata")
            changed = True
        elif metadata.get("speaker_contact_suggestions"):
            metadata.pop("speaker_contact_suggestions", None)
            metadata.pop("speaker_contact_suggestions_updated_at", None)
            session.processing_metadata = metadata
            flag_modified(session, "processing_metadata")
            changed = True

        if changed:
            session.participants = participants
            flag_modified(session, "participants")
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("speaker contact participant stamp swallowed error session=%s", getattr(session, "id", None))
    return summary


# Pooled-voiceprint tuning (v3.34.0). Short single-speaker turns make far
# better voiceprints than one long merged segment — a 5-minute "segment"
# averages crosstalk/echo/noise into mush (measured 0.05 self-similarity on
# a real call). We pool up to TOP_K turns in the [MIN_S, MAX_S] band.
_POOL_MIN_S = 2.0
_POOL_MAX_S = 45.0
_POOL_TOP_K = 5


def pooled_embedding(items: list[dict]) -> Optional[list[float]]:
    """Pool one voiceprint from several turns/segments.

    Each item: {"start", "end", "embedding", ...}. Picks up to
    ``_POOL_TOP_K`` longest turns within the clean-duration band
    (``_POOL_MIN_S``..``_POOL_MAX_S`` seconds); if none are in band, falls
    back to the single longest usable turn (the old behaviour). Embeddings
    are L2-normalized, averaged, re-normalized — standard centroid pooling
    for cosine-similarity speaker embeddings. Returns None when no item
    carries an embedding.
    """
    usable: list[tuple[float, list[float]]] = []
    for it in items or []:
        emb = it.get("embedding")
        if not emb:
            continue
        try:
            dur = float(it.get("end") or 0) - float(it.get("start") or 0)
        except (TypeError, ValueError):
            continue
        if dur < 1.0:
            continue
        usable.append((dur, emb))
    if not usable:
        return None
    band = [u for u in usable if _POOL_MIN_S <= u[0] <= _POOL_MAX_S]
    picked = sorted(band, key=lambda u: u[0], reverse=True)[:_POOL_TOP_K] or [
        max(usable, key=lambda u: u[0])
    ]
    vecs = []
    for _, emb in picked:
        v = np.asarray(emb, dtype="float32")
        n = np.linalg.norm(v)
        if n > 0:
            vecs.append(v / n)
    if not vecs:
        return None
    mean = np.mean(vecs, axis=0)
    n = np.linalg.norm(mean)
    if n == 0:
        return None
    return (mean / n).tolist()


def identify_speakers(
    session: RecordingSession,
    db: Session,
    *,
    threshold: float = DEFAULT_IDENTIFY_THRESHOLD,
) -> dict:
    """Post-completion hook: walk the diarized segments, embed the longest
    sample of each unique label, and try to match it against enrolled speakers.

    Writes a `speaker_session_link` row per unique raw_label and rewrites the
    `transcript_diarized.segments[*].speaker` field with the matched display
    name when confidence is above threshold. Falls through silently if the
    session has no diarized output, no audio file, or the speaker-svc is
    unreachable — this is a non-blocking enrichment step."""
    summary = {
        "session_id": session.id,
        "linked": 0,
        "unmatched": 0,
        "skipped_reason": None,
        "backend": None,
    }

    segments = _segments_from_session(session)
    if not segments:
        summary["skipped_reason"] = "no_diarized_segments"
        return summary

    # Audio file is only required if we need to re-extract embeddings
    # ourselves; if the diarizer already wrote embeddings into each
    # segment we can identify without ever touching the wav file.
    has_inline_embeddings = any(seg.get("embedding") for seg in segments)
    audio_path = session.audio_file
    if not has_inline_embeddings:
        # We must re-extract embeddings from the wav. Resolve local-first, but
        # fall back to pulling the canonical audio from Garage if the local
        # working copy was evicted post-cutover. Only done here (never on the
        # common inline-embeddings path, which never touches the file).
        try:
            from services.session_media import resolve_local_path
            resolved = resolve_local_path(session)
            if resolved is not None:
                audio_path = str(resolved)
        except Exception:  # noqa: BLE001
            logger.warning("identify_speakers: Garage resolve failed for session=%s", session.id)
        if not audio_path or not os.path.exists(audio_path):
            summary["skipped_reason"] = f"audio_missing:{audio_path}"
            logger.info("identify_speakers skipped: %s", summary["skipped_reason"])
            return summary

    # Group segments by their RAW diarizer label (``raw_label`` survives the
    # "Speaker N" display normalization, so a post-rename re-identify still
    # lines up with the diarizer's clusters). Pool one voiceprint per group:
    # prefer the diarizer's fine-grained turns persisted in
    # ``transcript_diarized["speaker_turns"]`` (v3.34.0) — the text segments
    # are coarse multi-minute merges whose single embedding is too mushy to
    # match anyone. Fallback: pool from the segments themselves (legacy
    # sessions without a turn bank degrade to the old longest-segment pick).
    diarized_doc = (
        session.transcript_diarized
        if isinstance(session.transcript_diarized, dict)
        else {}
    )
    turns_by_label: dict[str, list[dict]] = {}
    for turn in (diarized_doc.get("speaker_turns") or []):
        t_label = (turn.get("speaker") or "").strip()
        if t_label:
            turns_by_label.setdefault(t_label, []).append(turn)

    segs_by_label: dict[str, list[dict]] = {}
    for seg in segments:
        label = ((seg.get("raw_label") or seg.get("speaker")) or "").strip()
        if not label:
            continue
        duration = float(seg.get("end", 0)) - float(seg.get("start", 0))
        if duration < 0.5:
            continue
        segs_by_label.setdefault(label, []).append(seg)

    by_label: dict[str, dict] = {}
    for label, group in segs_by_label.items():
        emb = pooled_embedding(turns_by_label.get(label) or group)
        longest = max(
            (float(s.get("end", 0)) - float(s.get("start", 0)) for s in group),
            default=0.0,
        )
        by_label[label] = {
            "duration": longest,
            "segment": group[0],
            "embedding": emb,
        }

    if not by_label:
        summary["skipped_reason"] = "no_usable_segments"
        return summary

    # Resolve provider lazily so tests can monkeypatch it
    from services.providers.registry import get_provider_registry
    registry = get_provider_registry(db)
    provider = registry.get_speaker_svc(session.organization_id)

    # If the provider already returned per-segment embeddings, use them. If
    # not, embed each chosen segment via /embed (the slower path).
    candidates = candidates_for_org(db, session.organization_id)
    # "My Voice" (v3.36.0): account-level voiceprints of this org's MEMBERS.
    # Matched locally below — never sent through provider.identify (its
    # speaker_id plumbing is org SpeakerProfile ids).
    my_voice_candidates = my_voice_candidates_for_org(db, session.organization_id)
    label_to_speaker: dict[str, dict] = {}

    def _autocreate_if_eligible(label: str, item: dict, emb) -> Optional[SpeakerProfile]:
        """Persistent identity (v3.43): turn an unmatched voice into a stable
        UNNAMED profile (handle), unless it's too short or the label is already
        user-confirmed. Covers both the no-enrolled-speakers bootstrap case and
        the enrolled-but-no-match case."""
        if not (
            SPEAKER_AUTOCREATE_ENABLED
            and emb
            and float(item.get("duration", 0.0)) >= SPEAKER_AUTOCREATE_MIN_SECONDS
        ):
            return None
        already = (
            db.query(SpeakerSessionLink)
            .filter(
                SpeakerSessionLink.session_id == session.id,
                SpeakerSessionLink.raw_label == label,
                SpeakerSessionLink.confirmed.is_(True),
            )
            .first()
        )
        if already is not None:
            return None
        return auto_create_speaker(
            db, session.organization_id, emb, source_session_id=session.id
        )

    for label, item in by_label.items():
        emb = item.get("embedding")
        if not emb:
            # The diarize() call didn't include embeddings (e.g. fallback
            # backend). Skip — we'd need to re-extract the segment audio,
            # which the speaker-svc /embed endpoint doesn't currently do
            # from a substring of an audio_path.
            label_to_speaker[label] = {"matched": False, "reason": "no_embedding"}
            continue

        if not candidates and not my_voice_candidates:
            # No enrolled speakers yet — bootstrap the org's library by
            # auto-creating a persistent profile for this voice.
            auto_sp = _autocreate_if_eligible(label, item, emb)
            if auto_sp is not None:
                label_to_speaker[label] = {
                    "matched": True, "speaker_id": auto_sp.id,
                    "similarity": 1.0, "auto_created": True,
                }
                summary["auto_created"] = summary.get("auto_created", 0) + 1
            else:
                label_to_speaker[label] = {"matched": False, "reason": "no_enrolled_speakers"}
            continue

        # Org-library match via the provider (unchanged path).
        org_best: Optional[tuple[int, float]] = None  # (speaker_id, similarity)
        org_miss_reason = "below_threshold"
        if candidates:
            try:
                result = _run_async(provider.identify(emb, candidates, threshold))
            except Exception as exc:  # noqa: BLE001
                logger.warning("identify(label=%s) failed: %s", label, exc)
                result = None
                org_miss_reason = str(exc)
            best = result.get("best_match") if result else None
            if best and best.get("matched"):
                org_best = (best["speaker_id"], float(best["similarity"]))

        # My-voice match: LOCAL numpy cosine against decoded member centroids.
        # Per-candidate guarded — one bad row (empty decode, dim mismatch,
        # numpy error) must never abort the whole identify pass.
        mv_best: Optional[dict] = None
        mv_best_sim = -1.0
        for cand in my_voice_candidates:
            cand_emb = cand.get("embedding") or []
            if not cand_emb or len(cand_emb) != len(emb):
                logger.debug(
                    "my-voice candidate user=%s skipped (dim %s vs %s)",
                    cand.get("user_id"), len(cand_emb), len(emb),
                )
                continue
            try:
                sim = cosine_similarity(emb, cand_emb)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "my-voice cosine failed for user=%s (skipping candidate): %s",
                    cand.get("user_id"), exc,
                )
                continue
            if sim > mv_best_sim:
                mv_best_sim = sim
                mv_best = cand
        mv_match = mv_best if (mv_best is not None and mv_best_sim >= threshold) else None

        # A label the user already CONFIRMED gets ZERO my-voice side effects:
        # no SpeakerProfile bootstrap, no linked_user_id claim. The persist
        # loop below already refuses to overwrite confirmed links — this
        # guard stops the bootstrap mutations from happening at all (the
        # org-library result, if any, proceeds unchanged).
        if mv_match is not None:
            confirmed_link = (
                db.query(SpeakerSessionLink)
                .filter(
                    SpeakerSessionLink.session_id == session.id,
                    SpeakerSessionLink.raw_label == label,
                    SpeakerSessionLink.confirmed.is_(True),
                )
                .first()
            )
            if confirmed_link is not None:
                mv_match = None

        # Decision: whichever similarity is higher wins; tie -> org library.
        if org_best is not None and (mv_match is None or org_best[1] >= mv_best_sim):
            label_to_speaker[label] = {
                "matched": True,
                "speaker_id": org_best[0],
                "similarity": org_best[1],
            }
        elif mv_match is not None:
            user = mv_match["user"]
            speaker = _bootstrap_speaker_for_user(
                db, session.organization_id, user, mv_match
            )
            if speaker is not None:
                logger.info(
                    "my-voice match user=%s sim=%.3f -> speaker=%s",
                    user.id, mv_best_sim, speaker.id,
                )
                label_to_speaker[label] = {
                    "matched": True,
                    "speaker_id": speaker.id,
                    "similarity": mv_best_sim,
                }
            else:
                label_to_speaker[label] = {
                    "matched": False,
                    "reason": "my_voice_bootstrap_failed",
                }
        else:
            # Persistent identity (v3.43): no enrolled speaker matched — auto-
            # create a stable UNNAMED profile for this voice (closure above), so
            # the SAME voice matches it (same handle) in future meetings and
            # naming it later propagates everywhere.
            auto_sp = _autocreate_if_eligible(label, item, emb)
            if auto_sp is not None:
                label_to_speaker[label] = {
                    "matched": True,
                    "speaker_id": auto_sp.id,
                    "similarity": 1.0,
                    "auto_created": True,
                }
                summary["auto_created"] = summary.get("auto_created", 0) + 1
            else:
                label_to_speaker[label] = {"matched": False, "reason": org_miss_reason}

    # Persist links + rewrite diarized segments
    links_by_label: dict[str, SpeakerSessionLink] = {}
    for label, decision in label_to_speaker.items():
        link = (
            db.query(SpeakerSessionLink)
            .filter(
                SpeakerSessionLink.session_id == session.id,
                SpeakerSessionLink.raw_label == label,
            )
            .first()
        )
        if link is None:
            link = SpeakerSessionLink(
                session_id=session.id,
                organization_id=session.organization_id,
                raw_label=label,
            )
            db.add(link)
        if link.confirmed:
            # Manual override stays in place; we never overwrite a confirmed link.
            links_by_label[label] = link
            continue
        if decision.get("matched"):
            link.speaker_id = decision["speaker_id"]
            link.similarity = decision["similarity"]
            link.source = "auto"
            summary["linked"] += 1
        else:
            link.speaker_id = None
            link.similarity = None
            summary["unmatched"] += 1
        links_by_label[label] = link

    db.flush()

    # Prune stale UNCONFIRMED links left by earlier diarization passes — a
    # reprocess re-labels the clusters, and leftover links (e.g. SPEAKER_02
    # from the original pass) would surface phantom speakers in the naming
    # UI. Confirmed links carry user intent and are never pruned.
    valid_labels: set[str] = set(by_label.keys())
    for seg in segments:
        for k in ("raw_label", "speaker"):
            v = (seg.get(k) or "").strip()
            if v:
                valid_labels.add(v)
    pruned = 0
    for stale in (
        db.query(SpeakerSessionLink)
        .filter(
            SpeakerSessionLink.session_id == session.id,
            SpeakerSessionLink.confirmed.is_(False),
        )
        .all()
    ):
        if (stale.raw_label or "") not in valid_labels:
            db.delete(stale)
            pruned += 1
    if pruned:
        summary["pruned_stale_links"] = pruned

    # Build a label -> display_name map and rewrite the diarized transcript.
    name_map: dict[str, str] = {}
    for label, link in links_by_label.items():
        if link.speaker_id is None:
            continue
        sp = get_speaker(db, session.organization_id, link.speaker_id)
        if sp is not None:
            name_map[label] = sp.display_name

    if name_map:
        diarized = session.transcript_diarized or {}
        if isinstance(diarized, dict):
            new_segments = []
            for seg in diarized.get("segments", []):
                # Match on the RAW label first: post-normalization segments
                # display "Speaker N" while carrying the diarizer code in
                # raw_label — both must resolve to the matched name.
                key = ((seg.get("raw_label") or seg.get("speaker")) or "")
                if key in name_map:
                    new_seg = {**seg, "speaker": name_map[key], "raw_label": key}
                else:
                    new_seg = seg
                new_segments.append(new_seg)
            diarized["segments"] = new_segments
            # Refresh top-level speakers list if present
            if "speakers" in diarized:
                seen, ordered = set(), []
                for seg in new_segments:
                    s = seg.get("speaker", "Unknown")
                    if s not in seen:
                        seen.add(s)
                        ordered.append(s)
                diarized["speakers"] = ordered
            session.transcript_diarized = diarized
            # SQLAlchemy can't detect in-place mutations on JSON columns;
            # explicitly flag the column as dirty so the change persists.
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(session, "transcript_diarized")

    db.commit()
    backend_hint = (segments[0] or {}).get("backend") if segments else None
    summary["backend"] = backend_hint
    logger.info("identify_speakers: %s", summary)
    return summary
