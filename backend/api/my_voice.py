""""My Voice" — portable per-USER-account voiceprint API (v3.36.0).

Endpoints
---------
GET    /api/me/voice                      — enrollment status for the caller
DELETE /api/me/voice                      — destroy the caller's voiceprint (idempotent 204)
POST   /api/me/voice/enroll-from-session  — pool an embedding for one diarized
                                            label of a session the caller can
                                            access and fold it into their
                                            account voiceprint

Voiceprints are hard tenant-scoped by design (biometric privacy). The
user_voice_profile row is the single sanctioned exception: it belongs to the
ACCOUNT and participates in identification only inside workspaces the user
is a MEMBER of (see services.speaker_service.my_voice_candidates_for_org).
consent_at is the biometric consent record — stamped ONLY by explicit user
action: an is_me claim on a speaker-naming surface, or this module's
enroll-from-session endpoint. Nothing implicit (e.g. an email match) ever
enrolls a voiceprint.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from auth.tier import gate_feature_for_caller
from database.database import get_db
from database.models import RecordingSession, SpeakerProfile, UserVoiceProfile

logger = logging.getLogger(__name__)

router = APIRouter(tags=["My Voice"])


class MyVoiceStatus(BaseModel):
    enrolled: bool
    sample_count: int = 0
    embedding_model: Optional[str] = None
    updated_at: Optional[str] = None
    consent_at: Optional[str] = None


class EnrollFromSessionRequest(BaseModel):
    session_id: int
    raw_label: str


class EnrollFromSessionResponse(BaseModel):
    enrolled: bool
    sample_count: int


def _get_profile(db: Session, user_id: int) -> Optional[UserVoiceProfile]:
    return (
        db.query(UserVoiceProfile)
        .filter(UserVoiceProfile.user_id == user_id)
        .first()
    )


@router.get("/api/me/voice", response_model=MyVoiceStatus)
async def get_my_voice(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = _get_profile(db, current_user.id)
    if profile is None:
        return MyVoiceStatus(enrolled=False, sample_count=0)
    return MyVoiceStatus(
        enrolled=bool(profile.centroid_embedding),
        sample_count=profile.sample_count or 0,
        embedding_model=profile.embedding_model,
        updated_at=profile.updated_at.isoformat() if profile.updated_at else None,
        consent_at=profile.consent_at.isoformat() if profile.consent_at else None,
    )


@router.delete("/api/me/voice", status_code=204)
async def delete_my_voice(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Destroy the caller's account voiceprint. Idempotent — 204 even when
    no profile exists (biometric deletion must never error into retention).

    Destroys the account voiceprint and any centroids seeded from it:
    every org SpeakerProfile (across ALL orgs) linked to this user gets its
    centroid recomputed strictly from its own org-level SpeakerVoiceSample
    rows, so a pure my-voice-seeded bootstrap profile (no samples) is nulled
    instead of silently retaining the biometric. Org-level voice samples
    recorded in a workspace remain that workspace's data."""
    from services.speaker_service import recompute_speaker_from_samples

    profile = _get_profile(db, current_user.id)
    if profile is not None:
        db.delete(profile)

    # Purge account-voiceprint seeds from every linked org profile (the
    # bootstrap in identify_speakers copies the account centroid into org
    # SpeakerProfile rows; deleting only the account row would retain the
    # biometric there). Runs even on the idempotent no-profile path so a
    # retried delete still converges.
    linked = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.linked_user_id == current_user.id)
        .all()
    )
    for speaker in linked:
        recompute_speaker_from_samples(db, speaker)

    if profile is not None or linked:
        db.commit()
        logger.info(
            "my-voice: deleted voiceprint for user=%s (recomputed %d linked org profiles)",
            current_user.id, len(linked),
        )
    return Response(status_code=204)


@router.post("/api/me/voice/enroll-from-session", response_model=EnrollFromSessionResponse)
async def enroll_my_voice_from_session(
    payload: EnrollFromSessionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Enroll/extend the caller's "My Voice" voiceprint from one diarized
    cluster of a session in their active workspace. Pools the embedding from
    the session's fine-grained speaker_turns (fallback: the coarse text
    segments) — the exact turn-bank pooling the assign-confirm harvest uses.
    """
    # Same write gate as the speaker-library naming surfaces — enrollment
    # mutates persistent voiceprint state.
    gate_feature_for_caller(current_user, "speaker_library", active_org)

    # Same org-resolution/access pattern as api.speakers: the session must
    # live in the caller's active organization (membership enforced by
    # get_current_organization).
    session = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.organization_id == active_org.organization.id,
            RecordingSession.id == payload.session_id,
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    from api.session_permissions import has_session_access

    # A workspace viewer may read the meeting, but must not harvest its
    # biometric vectors into a personal voiceprint.  Require the same edit
    # permission used by speaker-label mutations before touching embeddings.
    if has_session_access(session.id, current_user, db) != "edit":
        raise HTTPException(status_code=404, detail="Session not found")

    raw_label = (payload.raw_label or "").strip()
    if not raw_label:
        raise HTTPException(status_code=400, detail="raw_label is required")

    from services.speaker_service import (
        DEFAULT_EMBEDDING_MODEL,
        fold_embedding_into_user_voice,
        pooled_embedding,
    )

    diarized = (
        session.transcript_diarized
        if isinstance(session.transcript_diarized, dict)
        else {}
    )
    turns = [
        t for t in (diarized.get("speaker_turns") or [])
        if (t.get("speaker") or "").strip() == raw_label
    ]
    segments = [
        seg for seg in (diarized.get("segments") or [])
        if ((seg.get("raw_label") or seg.get("speaker")) or "").strip() == raw_label
    ]
    emb = pooled_embedding(turns) or pooled_embedding(segments)
    if not emb:
        raise HTTPException(
            status_code=400,
            detail=f"No usable voice embedding for label '{raw_label}' in this session",
        )

    try:
        profile, status = fold_embedding_into_user_voice(
            db, current_user.id, emb, embedding_model=DEFAULT_EMBEDDING_MODEL
        )
    except IntegrityError:
        # Concurrent FIRST enrollments can race the SELECT inside the fold —
        # both miss, both INSERT, the loser hits uq_user_voice_profile_user.
        # Roll back and retry once: the second SELECT finds the winner's row.
        db.rollback()
        profile, status = fold_embedding_into_user_voice(
            db, current_user.id, emb, embedding_model=DEFAULT_EMBEDDING_MODEL
        )

    if status != "folded":
        # Consistency floor / dim guard (anti-poisoning): the pooled sample
        # doesn't look like the already-enrolled voiceprint, so we refuse to
        # average it in (and never restart the centroid).
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "This audio doesn't match your enrolled voice fingerprint. "
                "Clear your voiceprint first if you want to re-enroll."
            ),
        )

    db.commit()
    logger.info(
        "my-voice: enrolled from session=%s label=%s user=%s (samples now %s)",
        session.id, raw_label, current_user.id, profile.sample_count,
    )
    return EnrollFromSessionResponse(enrolled=True, sample_count=profile.sample_count or 0)
