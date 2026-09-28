"""Speaker Library API.

Endpoints
---------
GET    /api/speakers                              — list speakers in active org
POST   /api/speakers                              — create a speaker (admin)
GET    /api/speakers/{id}                         — get one speaker + samples
PATCH  /api/speakers/{id}                         — update display_name / email / notes (admin)
DELETE /api/speakers/{id}                         — delete a speaker + cascade samples (admin)
POST   /api/speakers/{id}/voice-samples           — upload a .wav clip, embed it, update centroid (admin)
DELETE /api/speakers/{id}/voice-samples/{sid}     — drop a sample, recompute centroid (admin)
GET    /api/sessions/{session_id}/speaker-links   — list raw_label -> speaker mappings
PATCH  /api/sessions/{session_id}/speaker-links/{link_id}
                                                  — confirm / change / clear a mapping (any user with org access)
POST   /api/sessions/{session_id}/identify-speakers
                                                  — re-run identification for a finished session

All routes are org-scoped via ActiveOrganization. Mutating endpoints
require admin role except for the link-confirm endpoint, which any org
member can use (it's the inline tagger workflow).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import AuditLog, User, UserOrganization
from auth.organization import ActiveOrganization
from auth.tier import gate_feature_for_caller
from database.database import get_db
from database.models import (
    RecordingSession,
    SpeakerProfile,
    SpeakerSessionLink,
    SpeakerVoiceSample,
    Transcription,
)
from services.providers.registry import get_provider_registry
from services.speaker_self_intro import extract_self_intro_names
from services.speaker_service import (
    DEFAULT_EMBEDDING_MODEL,
    STORE_SPEAKER_AUDIO,
    add_voice_sample,
    cosine_similarity,
    decode_embedding,
    encode_embedding,
    identify_speakers,
    recompute_speaker_from_samples,
    update_centroid,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Speakers"])

VOICE_SAMPLE_DIR = os.getenv("SPEAKER_AUDIO_DIR", "/app/recordings/speakers")


# ---------- response / request models ----------


class SpeakerResponse(BaseModel):
    id: int
    organization_id: int
    display_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    linked_user_id: Optional[int] = None
    external_refs: Optional[dict] = None
    contact_id: Optional[str] = None
    contact_link_confirmed: bool = False
    contact_link_confirmed_by_user_id: Optional[int] = None
    contact_link_confirmed_at: Optional[str] = None
    notes: Optional[str] = None
    embedding_dim: Optional[int] = None
    embedding_model: Optional[str] = None
    sample_count: int = 0
    has_centroid: bool = False
    # v3.43: True for an auto-created, not-yet-named persistent profile
    # (display_name is a stable handle like "Speaker 3F2A"); the UI surfaces
    # these for one-click naming, which then propagates to all past meetings.
    auto_generated: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SpeakerCreate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200)
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    linked_user_id: Optional[int] = None
    external_refs: Optional[dict] = None
    contact_id: Optional[str] = Field(default=None, max_length=64)
    contact_link_confirmed: bool = False
    notes: Optional[str] = None


class SpeakerUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    email: Optional[str] = Field(default=None, max_length=320)
    phone: Optional[str] = None
    title: Optional[str] = None
    company: Optional[str] = None
    linked_user_id: Optional[int] = None
    external_refs: Optional[dict] = None
    contact_id: Optional[str] = Field(default=None, max_length=64)
    contact_link_confirmed: Optional[bool] = None
    notes: Optional[str] = None


class VoiceSampleResponse(BaseModel):
    id: int
    speaker_id: int
    source: str
    source_session_id: Optional[int] = None
    embedding_dim: int
    embedding_model: str
    duration_seconds: Optional[float] = None
    similarity_to_centroid: Optional[float] = None
    has_audio: bool = False
    created_at: Optional[str] = None


class SpeakerDetailResponse(SpeakerResponse):
    voice_samples: list[VoiceSampleResponse] = Field(default_factory=list)
    session_count: int = 0


class SelfIntroName(BaseModel):
    """A one-click name suggestion derived from a speaker's OWN utterances
    (e.g. "Hi, I'm John"). Suggestion-only: the FE renders it as a chip the
    user confirms via the existing create-speaker / assign endpoints. Never
    auto-applied.
    """
    name: str
    evidence: str = ""  # the utterance the name came from (tooltip / audit)


class SpeakerLinkResponse(BaseModel):
    id: int
    session_id: int
    raw_label: str
    speaker_id: Optional[int] = None
    speaker_name: Optional[str] = None
    similarity: Optional[float] = None
    source: str
    confirmed: bool
    # v3.x self-intro suggestions: populated ONLY for unidentified links
    # (speaker_id is None). Empty for voice-matched / named / confirmed links.
    # Backward compatible (default []): older FE bundles ignore it.
    name_suggestions: list[SelfIntroName] = Field(default_factory=list)
    # v3.36.1 "My Voice" fold-status surfacing:
    #   True  = a sample was folded into the caller's account voiceprint
    #           during this request
    #   False = a fold was attempted but skipped (consistency floor / dim
    #           guard / no embedding)
    #   None  = no fold was applicable (no is_me claim and the speaker is
    #           not linked to the caller), or the best-effort attempt errored
    my_voice_folded: Optional[bool] = None


class SpeakerLinkUpdate(BaseModel):
    speaker_id: Optional[int] = None  # None / 0 to clear
    confirmed: bool = True
    # v3.36.0 "My Voice": the caller asserts THIS cluster is themself. Links
    # the speaker to the caller's account and folds the pooled session
    # embedding into their portable user_voice_profile.
    is_me: bool = False


class SpeakerContactLinkUpdate(BaseModel):
    contact_id: Optional[str] = Field(default=None, max_length=64)
    confirmed: bool = True


class IdentifyResponse(BaseModel):
    session_id: int
    linked: int
    unmatched: int
    skipped_reason: Optional[str] = None
    backend: Optional[str] = None


class SpeakerTopMatch(BaseModel):
    speaker_id: int
    display_name: str
    similarity: float


class UnassignedSpeakerLinkResponse(BaseModel):
    id: int
    raw_label: str
    session_id: str
    session_pk: int
    session_title: str
    session_started_at: Optional[str] = None
    meeting_date: Optional[str] = None
    duration_seconds: float = 0.0
    segment_count: int = 0
    preview: str = ""
    sample_audio_url: Optional[str] = None
    top_matches: list[SpeakerTopMatch] = Field(default_factory=list)
    # Text-derived sibling of top_matches: self-intro name suggestions scanned
    # from this cluster's OWN segments. Every row here is already speaker_id IS
    # NULL (the query filters on it), so all rows are eligible.
    name_suggestions: list[SelfIntroName] = Field(default_factory=list)


class UnassignedSpeakerLinksPage(BaseModel):
    items: list[UnassignedSpeakerLinkResponse]
    count: int
    total: int
    limit: int
    offset: int
    next_offset: Optional[int] = None


class SpeakerMergeRequest(BaseModel):
    source_id: int


# ---------- helpers ----------


def _to_response(speaker: SpeakerProfile) -> SpeakerResponse:
    return SpeakerResponse(
        id=speaker.id,
        organization_id=speaker.organization_id,
        display_name=speaker.display_name,
        email=speaker.email,
        phone=speaker.phone,
        title=speaker.title,
        company=speaker.company,
        linked_user_id=speaker.linked_user_id,
        external_refs=speaker.external_refs,
        contact_id=speaker.contact_id,
        contact_link_confirmed=bool(speaker.contact_link_confirmed),
        contact_link_confirmed_by_user_id=speaker.contact_link_confirmed_by_user_id,
        contact_link_confirmed_at=(
            speaker.contact_link_confirmed_at.isoformat()
            if speaker.contact_link_confirmed_at
            else None
        ),
        notes=speaker.notes,
        embedding_dim=speaker.embedding_dim,
        embedding_model=speaker.embedding_model,
        sample_count=speaker.sample_count or 0,
        has_centroid=speaker.centroid_embedding is not None,
        auto_generated=bool((speaker.external_refs or {}).get("auto_generated")),
        created_at=speaker.created_at.isoformat() if speaker.created_at else None,
        updated_at=speaker.updated_at.isoformat() if speaker.updated_at else None,
    )


def _to_sample_response(sample: SpeakerVoiceSample) -> VoiceSampleResponse:
    return VoiceSampleResponse(
        id=sample.id,
        speaker_id=sample.speaker_id,
        source=sample.source,
        source_session_id=sample.source_session_id,
        embedding_dim=sample.embedding_dim,
        embedding_model=sample.embedding_model,
        duration_seconds=sample.duration_seconds,
        similarity_to_centroid=sample.similarity_to_centroid,
        has_audio=bool(sample.audio_path),
        created_at=sample.created_at.isoformat() if sample.created_at else None,
    )


def _require_admin(active_org: ActiveOrganization, current_user: User) -> None:
    if current_user.is_superuser:
        return
    if active_org.role not in ("admin", "superuser"):
        raise HTTPException(status_code=403, detail="Speaker library management requires admin role.")


def _get_speaker_or_404(db: Session, organization_id: int, speaker_id: int) -> SpeakerProfile:
    speaker = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.organization_id == organization_id,
            SpeakerProfile.id == speaker_id,
        )
        .first()
    )
    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")
    return speaker


def _get_session_or_404(
    db: Session,
    organization_id: int,
    session_id: int | str,
    current_user: User,
    *,
    min_level: str = "view",
    require_speaker_editor: bool = False,
) -> RecordingSession:
    q = db.query(RecordingSession).filter(RecordingSession.organization_id == organization_id)
    try:
        sid = int(session_id)
        session = q.filter(RecordingSession.id == sid).first()
    except (TypeError, ValueError):
        session = q.filter(RecordingSession.session_id == str(session_id)).first()

    # Fallback: the session may live in a DIFFERENT org while the caller
    # still has access (e.g. a personal-org recording viewed while active
    # in another org, or a per-meeting collaborator). Mirror get_session's
    # resolution — resolve cross-org and delegate the final permission
    # check to has_session_access. Imported inside the function to avoid a
    # circular import, exactly as get_session does.
    from api.session_permissions import _get_session_by_str_id, has_session_access

    if not session:
        candidate = _get_session_by_str_id(db, str(session_id))
        if candidate:
            session = candidate

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Resolve the caller's actual permission even when the row was found in
    # the active org.  Active-org viewers are read-only, and cross-org
    # collaborators may have view/comment/edit grants.  Speaker relabeling,
    # enrollment, merging and re-diarization all mutate meeting or biometric
    # state, so their callers pass min_level="edit".
    ranks = {"denied": 0, "view": 1, "comment": 2, "edit": 3}
    level = has_session_access(session.id, current_user, db)
    if ranks.get(level, 0) < ranks.get(min_level, 1):
        # Keep the existing non-disclosure behavior: callers without enough
        # access should not be able to confirm that another meeting exists.
        raise HTTPException(status_code=404, detail="Session not found")
    if require_speaker_editor and not _can_edit_speaker_state(db, session, current_user):
        raise HTTPException(status_code=404, detail="Session not found")
    return session


def _can_edit_speaker_state(
    db: Session,
    session: RecordingSession,
    current_user: User,
) -> bool:
    """Limit biometric/speaker mutations to members of the host workspace."""
    if current_user.is_superuser:
        return True
    membership = (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id == current_user.id,
            UserOrganization.organization_id == session.organization_id,
        )
        .first()
    )
    return bool(
        membership
        and membership.role in {"admin", "manager", "member", "user"}
    )


async def _enqueue_summary_refresh(session: RecordingSession, db: Session) -> None:
    """Fire-and-forget summary refresh after a speaker rename/enroll.

    Renaming already rewrites the transcript labels in place — the only
    stale artifact is the SUMMARY (it still says "Speaker 1"). Re-running
    the full reprocess for that re-does STT + diarization (~3-4 min of GPU)
    for nothing. Instead we enqueue ``finalize_session_job``, which runs
    identify -> normalize -> summarize -> insights WITHOUT re-transcribing
    (~40-60 s). The summary idempotency hash folds the speaker names into
    its input, so a rename genuinely re-runs the LLM, while a no-op rename
    skips it. Best-effort: a queue hiccup must never fail the rename.

    CRITICAL: we must stamp ``session.processing_job_id`` with the new job
    id — finalize_session_job's drift-guard skips any job whose id doesn't
    match the stamp, and completed sessions still carry the STALE id from
    their original finalize. Without the stamp every refresh silently
    drift-skips (found live on session 510).
    """
    try:
        if (session.status or "") != "completed":
            return  # an in-flight pipeline will summarize with the new name anyway
        from services.job_runner import enqueue_job

        job_id = await enqueue_job(
            "finalize_session_job",
            session.id,
            owner_user_id=session.user_id,
            owner_org_id=session.organization_id,
        )
        if job_id:
            session.processing_job_id = job_id
            db.add(session)
            db.commit()
        logger.info(
            "speaker rename: enqueued summary refresh job=%s session=%s",
            job_id, session.id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "speaker rename: summary refresh enqueue failed session=%s: %s",
            session.id, exc,
        )


def _segments_for_label(session: RecordingSession, raw_label: str) -> list[dict]:
    diarized = session.transcript_diarized if isinstance(session.transcript_diarized, dict) else {}
    segments = diarized.get("segments", []) if isinstance(diarized, dict) else []
    out: list[dict] = []
    for seg in segments:
        label = (seg.get("raw_label") or seg.get("speaker") or "").strip()
        if label == raw_label:
            out.append(seg)
    return out


def _apply_is_me_claim(speaker: SpeakerProfile, current_user: User) -> None:
    """is_me assertion: bind this SpeakerProfile to the caller's account.

    409 when the profile already belongs to a DIFFERENT user — "this voice
    is me" can never silently steal someone else's identity link.
    """
    if speaker.linked_user_id is not None and speaker.linked_user_id != current_user.id:
        raise HTTPException(
            status_code=409,
            detail="This speaker is already linked to a different user account.",
        )
    speaker.linked_user_id = current_user.id
    speaker.email = speaker.email or current_user.email


def _maybe_fold_my_voice(
    db: Session,
    current_user: User,
    speaker: SpeakerProfile,
    emb: Optional[list[float]],
    *,
    is_me: bool,
) -> Optional[str]:
    """Best-effort (never raises): fold a pooled session embedding into the
    CALLER's account-level user_voice_profile ("My Voice", v3.36.0).

    Biometric enrollment requires an EXPLICIT claim (v3.36.0 hardening): the
    fold runs only when the caller asserts is_me on THIS request, or the
    profile is already linked to the caller (speaker.linked_user_id ==
    current_user.id — i.e. a previous explicit is_me claim; refinement then
    continues). An email match alone is deliberately NOT sufficient —
    speaker emails are free-form org data and must never trigger enrollment
    of the caller's account voiceprint. Folds skipped by the consistency
    floor / dim guard in fold_embedding_into_user_voice stay silent here
    (the explicit /api/me/voice/enroll-from-session endpoint surfaces them
    as 409).

    Returns (v3.36.1 fold-status surfacing):
      "folded"  — a sample was folded into the caller's voiceprint
      "skipped" — a fold was applicable but skipped (consistency floor /
                  dim guard / no usable embedding)
      None      — not applicable (no self-claim), or the best-effort
                  attempt errored
    """
    try:
        if not (is_me or speaker.linked_user_id == current_user.id):
            return None
        if not emb:
            logger.info(
                "my-voice fold skipped (no embedding) user=%s (from speaker=%s)",
                current_user.id, speaker.id,
            )
            return "skipped"
        from services.speaker_service import fold_embedding_into_user_voice

        profile, status = fold_embedding_into_user_voice(
            db,
            current_user.id,
            emb,
            embedding_model=speaker.embedding_model or DEFAULT_EMBEDDING_MODEL,
        )
        if status == "folded":
            logger.info(
                "my-voice fold: user=%s samples=%s (from speaker=%s)",
                current_user.id, profile.sample_count, speaker.id,
            )
            return "folded"
        logger.warning(
            "my-voice fold skipped (%s) user=%s (from speaker=%s)",
            status, current_user.id, speaker.id,
        )
        return "skipped"
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "my-voice fold failed (non-fatal) user=%s: %s", current_user.id, exc
        )
        return None


def _fold_status_to_flag(status: Optional[str]) -> Optional[bool]:
    """Map _maybe_fold_my_voice's status onto the API contract:
    "folded" -> True, "skipped" -> False, None -> None."""
    if status == "folded":
        return True
    if status == "skipped":
        return False
    return None


def _segment_duration(seg: dict) -> float:
    try:
        return max(0.0, float(seg.get("end") or 0) - float(seg.get("start") or 0))
    except (TypeError, ValueError):
        return 0.0


def _cluster_centroid(segments: list[dict]) -> Optional[list[float]]:
    vectors: list[list[float]] = []
    for seg in segments:
        emb = seg.get("embedding")
        if isinstance(emb, list) and emb:
            vectors.append([float(v) for v in emb])
    if not vectors:
        return None
    centroid: Optional[list[float]] = None
    for idx, vec in enumerate(vectors):
        centroid = update_centroid(centroid, idx, vec)
    return centroid


def _first_preview(segments: list[dict], max_chars: int = 120) -> str:
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        if text:
            return text[:max_chars]
    return ""


def _top_matches_for_centroid(
    db: Session,
    organization_id: int,
    centroid: Optional[list[float]],
) -> list[SpeakerTopMatch]:
    if not centroid:
        return []
    rows = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.organization_id == organization_id,
            SpeakerProfile.centroid_embedding.isnot(None),
            SpeakerProfile.embedding_dim.isnot(None),
        )
        .all()
    )
    scored: list[SpeakerTopMatch] = []
    for speaker in rows:
        try:
            speaker_centroid = decode_embedding(speaker.centroid_embedding, speaker.embedding_dim)
            scored.append(SpeakerTopMatch(
                speaker_id=speaker.id,
                display_name=speaker.display_name,
                similarity=round(cosine_similarity(centroid, speaker_centroid), 4),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Skipping speaker match candidate %s: %s", speaker.id, exc)
    scored.sort(key=lambda item: item.similarity, reverse=True)
    return scored[:3]


def _delete_merged_source(db: Session, source: SpeakerProfile) -> None:
    db.delete(source)


# ---------- speaker library ----------


@router.get("/api/speakers", response_model=list[SpeakerResponse])
async def list_speakers(
    session_id: Optional[str] = Query(
        default=None,
        description="When set, list candidates from that meeting's workspace.",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    organization_id = active_org.organization.id
    if session_id:
        session = _get_session_or_404(
            db,
            active_org.organization.id,
            session_id,
            current_user,
            min_level="edit",
            require_speaker_editor=True,
        )
        organization_id = session.organization_id
    rows = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.organization_id == organization_id)
        .order_by(SpeakerProfile.display_name.asc())
        .all()
    )
    return [_to_response(r) for r in rows]


@router.get("/api/speakers/unassigned-links", response_model=UnassignedSpeakerLinksPage)
async def list_unassigned_speaker_links(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    limit = max(1, min(200, int(limit or 50)))
    offset = max(0, int(offset or 0))
    org_id = active_org.organization.id

    total = (
        db.query(func.count(SpeakerSessionLink.id))
        .filter(
            SpeakerSessionLink.organization_id == org_id,
            SpeakerSessionLink.speaker_id.is_(None),
        )
        .scalar()
        or 0
    )
    rows = (
        db.query(SpeakerSessionLink, RecordingSession)
        .join(RecordingSession, RecordingSession.id == SpeakerSessionLink.session_id)
        .filter(
            SpeakerSessionLink.organization_id == org_id,
            RecordingSession.organization_id == org_id,
            SpeakerSessionLink.speaker_id.is_(None),
        )
        .order_by(RecordingSession.started_at.desc().nullslast(), RecordingSession.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    items: list[UnassignedSpeakerLinkResponse] = []
    for link, session in rows:
        segments = _segments_for_label(session, link.raw_label)
        duration = sum(_segment_duration(seg) for seg in segments)
        centroid = _cluster_centroid(segments)

        sample_audio_url = None
        if STORE_SPEAKER_AUDIO:
            for seg in segments:
                idx = seg.get("idx") or seg.get("segment_idx") or seg.get("index")
                if idx is None:
                    continue
                try:
                    source_idx = int(idx)
                except (TypeError, ValueError):
                    continue
                sample = (
                    db.query(SpeakerVoiceSample)
                    .filter(
                        SpeakerVoiceSample.organization_id == org_id,
                        SpeakerVoiceSample.source_session_id == session.id,
                        SpeakerVoiceSample.source_segment_idx == source_idx,
                        SpeakerVoiceSample.audio_path.isnot(None),
                    )
                    .first()
                )
                if sample and sample.audio_path:
                    sample_audio_url = f"/api/speakers/voice-samples/{sample.id}/audio"
                    break

        items.append(UnassignedSpeakerLinkResponse(
            id=link.id,
            raw_label=link.raw_label,
            session_id=session.session_id or str(session.id),
            session_pk=session.id,
            session_title=session.title or session.name or f"Session {session.id}",
            session_started_at=session.started_at.isoformat() if session.started_at else None,
            meeting_date=session.meeting_date.isoformat() if session.meeting_date else None,
            duration_seconds=round(duration, 3),
            segment_count=len(segments),
            preview=_first_preview(segments),
            sample_audio_url=sample_audio_url,
            top_matches=_top_matches_for_centroid(db, org_id, centroid),
            # Self-intro suggestions from THIS cluster's own segment texts
            # only (segments already loaded above — zero extra DB work).
            name_suggestions=[
                SelfIntroName(**s)
                for s in extract_self_intro_names(
                    [str(seg.get("text") or "") for seg in segments]
                )
            ],
        ))

    next_offset = offset + len(items) if offset + len(items) < total else None
    return UnassignedSpeakerLinksPage(
        items=items,
        count=len(items),
        total=total,
        limit=limit,
        offset=offset,
        next_offset=next_offset,
    )


@router.get("/api/speakers/voice-samples/{sample_id}/audio")
async def get_voice_sample_audio(
    sample_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    sample = (
        db.query(SpeakerVoiceSample)
        .filter(
            SpeakerVoiceSample.id == sample_id,
            SpeakerVoiceSample.organization_id == active_org.organization.id,
        )
        .first()
    )
    if not sample or not sample.audio_path:
        raise HTTPException(status_code=404, detail="Voice sample audio not found")
    if not os.path.exists(sample.audio_path):
        raise HTTPException(status_code=404, detail="Voice sample audio missing on disk")
    return FileResponse(sample.audio_path, media_type="audio/wav")


@router.post("/api/speakers", response_model=SpeakerResponse, status_code=201)
async def create_speaker(
    payload: SpeakerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    # v3.18.3: speaker_library write paths gated by tier. Read paths
    # (list/get) stay open so a downgraded org doesn't lose visibility.
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    _require_admin(active_org, current_user)
    existing = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.organization_id == active_org.organization.id,
            SpeakerProfile.display_name == payload.display_name,
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=409, detail=f"Speaker '{payload.display_name}' already exists")
    speaker = SpeakerProfile(
        organization_id=active_org.organization.id,
        display_name=payload.display_name,
        email=payload.email,
        phone=payload.phone,
        title=payload.title,
        company=payload.company,
        linked_user_id=payload.linked_user_id,
        external_refs=payload.external_refs,
        contact_id=(payload.contact_id.strip() if payload.contact_id else None),
        contact_link_confirmed=bool(payload.contact_id and payload.contact_link_confirmed),
        contact_link_confirmed_by_user_id=(
            current_user.id if payload.contact_id and payload.contact_link_confirmed else None
        ),
        contact_link_confirmed_at=(
            datetime.now(timezone.utc) if payload.contact_id and payload.contact_link_confirmed else None
        ),
        notes=payload.notes,
        created_by_user_id=current_user.id,
    )
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    return _to_response(speaker)


@router.get("/api/speakers/{speaker_id}", response_model=SpeakerDetailResponse)
async def get_speaker(
    speaker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    speaker = _get_speaker_or_404(db, active_org.organization.id, speaker_id)
    samples = (
        db.query(SpeakerVoiceSample)
        .filter(SpeakerVoiceSample.speaker_id == speaker.id)
        .order_by(SpeakerVoiceSample.id.desc())
        .all()
    )
    session_count = (
        db.query(SpeakerSessionLink)
        .filter(
            SpeakerSessionLink.speaker_id == speaker.id,
            SpeakerSessionLink.organization_id == active_org.organization.id,
        )
        .count()
    )
    base = _to_response(speaker).model_dump()
    return SpeakerDetailResponse(
        **base,
        voice_samples=[_to_sample_response(s) for s in samples],
        session_count=session_count,
    )


@router.patch("/api/speakers/{speaker_id}", response_model=SpeakerResponse)
async def update_speaker(
    speaker_id: int,
    payload: SpeakerUpdate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    # v3.18.3: speaker_library write gate.
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    _require_admin(active_org, current_user)
    speaker = _get_speaker_or_404(db, active_org.organization.id, speaker_id)
    old_display_name = speaker.display_name
    renamed = False
    if payload.display_name is not None and payload.display_name != old_display_name:
        speaker.display_name = payload.display_name
        renamed = True
        # Naming an auto-created profile makes it user-owned — clear the flag so
        # the UI stops flagging it as unconfirmed.
        refs = dict(speaker.external_refs or {})
        if refs.pop("auto_generated", None) is not None:
            from sqlalchemy.orm.attributes import flag_modified
            speaker.external_refs = refs or None
            flag_modified(speaker, "external_refs")
    if payload.email is not None:
        speaker.email = payload.email or None
    if payload.notes is not None:
        speaker.notes = payload.notes or None
    # Optional contact fields (phone/title/company/linked_user_id/external_refs).
    # Pydantic gives us None when unset, so a real value overrides only when present.
    for fld in ("phone", "title", "company", "linked_user_id", "external_refs"):
        val = getattr(payload, fld, None)
        if val is not None:
            setattr(speaker, fld, val or None if isinstance(val, str) else val)
    if "contact_id" in payload.model_fields_set:
        speaker.contact_id = (payload.contact_id.strip() if payload.contact_id else None)
        if not speaker.contact_id:
            speaker.contact_link_confirmed = False
            speaker.contact_link_confirmed_by_user_id = None
            speaker.contact_link_confirmed_at = None
    if "contact_link_confirmed" in payload.model_fields_set:
        speaker.contact_link_confirmed = bool(payload.contact_link_confirmed and speaker.contact_id)
        speaker.contact_link_confirmed_by_user_id = current_user.id if speaker.contact_link_confirmed else None
        speaker.contact_link_confirmed_at = datetime.now(timezone.utc) if speaker.contact_link_confirmed else None
    db.commit()
    db.refresh(speaker)
    # A (v3.43): a rename propagates to every past meeting this speaker appears
    # in (transcript labels + summary text), so naming a once-anonymous voice
    # retroactively corrects history. Cheap + best-effort — never fails the rename.
    if renamed:
        # Propagate the rename to past meetings OFF the request path so the rename
        # returns instantly even for a speaker in many meetings. The session-detail
        # view renders names live (hydrate_diarized_speaker_names) so the UI is
        # correct immediately; this background pass updates the stored transcripts
        # + summaries for exports/chat/index.
        background_tasks.add_task(
            _propagate_rename_bg, speaker.id, active_org.organization.id, old_display_name
        )
    return _to_response(speaker)


@router.post("/api/speakers/{speaker_id}/resummarize-history")
async def resummarize_speaker_history(
    speaker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Re-generate the AI summary of every PAST completed meeting this speaker
    appears in, so summaries reflect the (now-named) speaker. Enqueues a
    lightweight finalize (identify -> summarize, NO re-STT/re-diarize, ~40-60s)
    per linked session. The transcript labels are already corrected on rename
    (apply_rename_to_history); this is the optional 'regenerate the summary too'
    button. Returns how many were enqueued."""
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    _require_admin(active_org, current_user)
    speaker = _get_speaker_or_404(db, active_org.organization.id, speaker_id)
    session_ids = [
        row[0]
        for row in db.query(SpeakerSessionLink.session_id)
        .filter(SpeakerSessionLink.speaker_id == speaker.id)
        .distinct()
        .all()
    ]
    enqueued = 0
    for sid in session_ids:
        session = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.id == sid,
                RecordingSession.organization_id == active_org.organization.id,
            )
            .first()
        )
        if session is None or (session.status or "") != "completed":
            continue
        await _enqueue_summary_refresh(session, db)
        enqueued += 1
    logger.info("resummarize_speaker_history: speaker=%s enqueued=%d/%d",
                speaker.id, enqueued, len(session_ids))
    return {
        "speaker_id": speaker.id,
        "display_name": speaker.display_name,
        "sessions_total": len(session_ids),
        "resummaries_enqueued": enqueued,
    }


@router.patch("/api/speakers/{speaker_id}/contact-link", response_model=SpeakerResponse)
async def update_speaker_contact_link(
    speaker_id: int,
    payload: SpeakerContactLinkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Confirm or clear a SpeakerProfile -> Contact-Ops person link."""
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    _require_admin(active_org, current_user)
    speaker = _get_speaker_or_404(db, active_org.organization.id, speaker_id)
    speaker.contact_id = (payload.contact_id.strip() if payload.contact_id else None)
    speaker.contact_link_confirmed = bool(speaker.contact_id and payload.confirmed)
    speaker.contact_link_confirmed_by_user_id = current_user.id if speaker.contact_link_confirmed else None
    speaker.contact_link_confirmed_at = datetime.now(timezone.utc) if speaker.contact_link_confirmed else None
    db.commit()
    db.refresh(speaker)
    return _to_response(speaker)


@router.delete("/api/speakers/{speaker_id}", status_code=204)
async def delete_speaker(
    speaker_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    # v3.18.3: speaker_library write gate.
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    _require_admin(active_org, current_user)
    speaker = _get_speaker_or_404(db, active_org.organization.id, speaker_id)
    # Detach session links so the deletion is a pure soft-unlink for transcripts.
    db.query(SpeakerSessionLink).filter(SpeakerSessionLink.speaker_id == speaker.id).update(
        {"speaker_id": None, "similarity": None, "confirmed": False}
    )
    db.query(SpeakerVoiceSample).filter(SpeakerVoiceSample.speaker_id == speaker.id).delete()
    db.delete(speaker)
    db.commit()


# ---------- voice sample enrollment ----------


@router.post("/api/speakers/{speaker_id}/voice-samples", response_model=VoiceSampleResponse, status_code=201)
async def upload_voice_sample(
    speaker_id: int,
    audio: UploadFile = File(..., description="3-30s clean speech sample (wav/flac/ogg/mp3)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    # v3.18.3: speaker_library write gate.
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    _require_admin(active_org, current_user)
    speaker = _get_speaker_or_404(db, active_org.organization.id, speaker_id)

    audio_bytes = await audio.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio upload")

    registry = get_provider_registry(db)
    provider = registry.get_speaker_svc(active_org.organization.id)
    embed_result = await provider.embed_bytes(audio_bytes, filename=audio.filename or "sample.wav")
    embedding = embed_result.get("embedding") or []
    if not embedding:
        raise HTTPException(status_code=502, detail="speaker-svc returned no embedding (check service health)")

    embedding_model = embed_result.get("model") or DEFAULT_EMBEDDING_MODEL
    duration = embed_result.get("duration_seconds")

    audio_path: Optional[str] = None
    if STORE_SPEAKER_AUDIO:
        os.makedirs(VOICE_SAMPLE_DIR, exist_ok=True)
        ext = (os.path.splitext(audio.filename or "")[1] or ".wav").lower()
        audio_path = os.path.join(VOICE_SAMPLE_DIR, f"speaker_{speaker.id}_{int((duration or 0) * 1000)}_{os.urandom(4).hex()}{ext}")
        try:
            with open(audio_path, "wb") as fh:
                fh.write(audio_bytes)
        except OSError as exc:
            logger.warning("Failed to persist voice sample audio: %s", exc)
            audio_path = None

    sample = add_voice_sample(
        db,
        speaker,
        embedding,
        source="enrollment",
        embedding_model=embedding_model,
        audio_path=audio_path,
        duration_seconds=duration,
    )
    db.commit()
    db.refresh(sample)
    return _to_sample_response(sample)


@router.delete("/api/speakers/{speaker_id}/voice-samples/{sample_id}", status_code=204)
async def delete_voice_sample(
    speaker_id: int,
    sample_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    # v3.18.3: speaker_library write gate.
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    _require_admin(active_org, current_user)
    speaker = _get_speaker_or_404(db, active_org.organization.id, speaker_id)
    sample = (
        db.query(SpeakerVoiceSample)
        .filter(
            SpeakerVoiceSample.id == sample_id,
            SpeakerVoiceSample.speaker_id == speaker.id,
        )
        .first()
    )
    if not sample:
        raise HTTPException(status_code=404, detail="Voice sample not found")

    if sample.audio_path and os.path.exists(sample.audio_path):
        try:
            os.remove(sample.audio_path)
        except OSError:
            pass
    db.delete(sample)
    db.flush()

    # Recompute centroid from remaining samples (or clear it if none remain).
    remaining = (
        db.query(SpeakerVoiceSample)
        .filter(SpeakerVoiceSample.speaker_id == speaker.id)
        .all()
    )
    if not remaining:
        speaker.centroid_embedding = None
        speaker.embedding_dim = None
        speaker.embedding_model = None
        speaker.sample_count = 0
    else:
        new_centroid: Optional[list[float]] = None
        for i, s in enumerate(remaining):
            vec = decode_embedding(s.embedding, s.embedding_dim)
            new_centroid = update_centroid(new_centroid, i, vec)
        if new_centroid is not None:
            speaker.centroid_embedding = encode_embedding(new_centroid)
            speaker.embedding_dim = len(new_centroid)
        speaker.sample_count = len(remaining)
    db.commit()


@router.post("/api/speakers/{target_id}/merge", response_model=SpeakerResponse)
async def merge_speaker_profile(
    target_id: int,
    payload: SpeakerMergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    # v3.18.3: speaker_library write gate.
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    _require_admin(active_org, current_user)
    org_id = active_org.organization.id
    source_id = payload.source_id
    if target_id == source_id:
        raise HTTPException(status_code=400, detail="Cannot merge a speaker into itself")

    target = _get_speaker_or_404(db, org_id, target_id)
    source = _get_speaker_or_404(db, org_id, source_id)
    source_name = source.display_name

    try:
        samples_moved = (
            db.query(SpeakerVoiceSample)
            .filter(
                SpeakerVoiceSample.organization_id == org_id,
                SpeakerVoiceSample.speaker_id == source.id,
            )
            .update({"speaker_id": target.id}, synchronize_session=False)
        )
        links_moved = (
            db.query(SpeakerSessionLink)
            .filter(
                SpeakerSessionLink.organization_id == org_id,
                SpeakerSessionLink.speaker_id == source.id,
            )
            .update({"speaker_id": target.id}, synchronize_session=False)
        )
        db.flush()
        recompute_speaker_from_samples(db, target)
        _delete_merged_source(db, source)
        db.add(AuditLog(
            user_id=current_user.id,
            organization_id=org_id,
            action="speaker_merge",
            resource_type="speaker",
            resource_id=str(target.id),
            details={
                "merged_from": source_id,
                "source_name": source_name,
                "samples_moved": samples_moved,
                "links_moved": links_moved,
            },
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("speaker merge failed target=%s source=%s", target_id, source_id)
        raise HTTPException(status_code=500, detail="Speaker merge failed")

    db.refresh(target)
    return _to_response(target)


# ---------- session linking ----------


def _link_to_response(
    db: Session,
    link: SpeakerSessionLink,
    *,
    my_voice_folded: Optional[bool] = None,
    name_suggestions: Optional[list[SelfIntroName]] = None,
) -> SpeakerLinkResponse:
    # name_suggestions is passed by the caller ONLY for unidentified links
    # (link.speaker_id is None); assigned/named links pass nothing -> []. The
    # helper stays free of extraction logic so it remains cheap.
    name = None
    if link.speaker_id:
        sp = db.query(SpeakerProfile).filter(SpeakerProfile.id == link.speaker_id).first()
        if sp:
            name = sp.display_name
    return SpeakerLinkResponse(
        id=link.id,
        session_id=link.session_id,
        raw_label=link.raw_label,
        speaker_id=link.speaker_id,
        speaker_name=name,
        similarity=link.similarity,
        source=link.source,
        confirmed=link.confirmed,
        my_voice_folded=my_voice_folded,
        name_suggestions=name_suggestions or [],
    )


@router.get("/api/sessions/{session_id}/speaker-links", response_model=list[SpeakerLinkResponse])
async def list_speaker_links(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """List speaker-session links for this session.

    If no links exist yet but the diarized transcript has labeled
    segments (SPEAKER_01, SPEAKER_02, etc.), auto-create one unconfirmed
    link per unique label so the inline tagger has something to attach
    names to. This makes the UI immediately useful after a fresh upload
    without first having to click Re-identify.
    """
    session = _get_session_or_404(
        db,
        active_org.organization.id,
        session_id,
        current_user,
    )
    links = (
        db.query(SpeakerSessionLink)
        .filter(SpeakerSessionLink.session_id == session.id)
        .order_by(SpeakerSessionLink.raw_label.asc())
        .all()
    )

    # Keep this GET read-only for viewers.  Missing rows are materialized only
    # for host-workspace editors; read/comment collaborators can still inspect
    # any existing labels without causing a database write.
    if not links and _can_edit_speaker_state(db, session, current_user):
        diarized = session.transcript_diarized if isinstance(session.transcript_diarized, dict) else {}
        segs = diarized.get("segments", []) if isinstance(diarized, dict) else []
        seen: set[str] = set()
        ordered_labels: list[str] = []
        for seg in segs:
            label = (seg.get("raw_label") or seg.get("speaker") or "").strip()
            if not label or label in seen:
                continue
            seen.add(label)
            ordered_labels.append(label)

        for label in ordered_labels:
            link = SpeakerSessionLink(
                session_id=session.id,
                organization_id=session.organization_id,
                raw_label=label,
            )
            db.add(link)
        if ordered_labels:
            db.commit()
            links = (
                db.query(SpeakerSessionLink)
                .filter(SpeakerSessionLink.session_id == session.id)
                .order_by(SpeakerSessionLink.raw_label.asc())
                .all()
            )

    # Build ONE raw_label -> [segment texts] map in a single pass over the
    # diarized segments, so we don't call _segments_for_label per link (it
    # re-scans every segment each call). Self-intro suggestions are computed
    # ONLY for unidentified links (speaker_id is None) from that cluster's OWN
    # texts — an intro spoken by another cluster is structurally unreachable.
    has_unassigned = any(link.speaker_id is None for link in links)
    label_texts: dict[str, list[str]] = {}
    if has_unassigned:
        diarized = session.transcript_diarized if isinstance(session.transcript_diarized, dict) else {}
        for seg in (diarized.get("segments", []) if isinstance(diarized, dict) else []):
            label = (seg.get("raw_label") or seg.get("speaker") or "").strip()
            if not label:
                continue
            label_texts.setdefault(label, []).append(str(seg.get("text") or ""))

    out: list[SpeakerLinkResponse] = []
    for link in links:
        suggestions: Optional[list[SelfIntroName]] = None
        if link.speaker_id is None:
            suggestions = [
                SelfIntroName(**s)
                for s in extract_self_intro_names(label_texts.get(link.raw_label, []))
            ]
        out.append(_link_to_response(db, link, name_suggestions=suggestions))
    return out


@router.patch("/api/sessions/{session_id}/speaker-links/{link_id}", response_model=SpeakerLinkResponse)
async def update_speaker_link(
    session_id: str,
    link_id: int,
    payload: SpeakerLinkUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Confirm (or change) the mapping from a raw_label to a Speaker.
    Accessible to any org member — this is the inline-tagger workflow."""
    # v3.18.3: speaker_library write gate. Tagging a raw label to a Speaker
    # mutates persistent library state, so free users (no speaker_library)
    # don't get it.
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    session = _get_session_or_404(
        db,
        active_org.organization.id,
        session_id,
        current_user,
        min_level="edit",
        require_speaker_editor=True,
    )
    link = (
        db.query(SpeakerSessionLink)
        .filter(
            SpeakerSessionLink.id == link_id,
            SpeakerSessionLink.session_id == session.id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Speaker link not found")

    if payload.speaker_id:
        # Session-scoped: the speaker must live in the SESSION's org (the
        # meeting's workspace), not the caller's active workspace — see the
        # create_speaker_for_link comment (v3.34.0 god-view fix).
        speaker = _get_speaker_or_404(db, session.organization_id, payload.speaker_id)
        if payload.is_me:
            # v3.36.0 "My Voice": bind the profile to the caller (409 if it
            # already belongs to someone else).
            _apply_is_me_claim(speaker, current_user)
        link.speaker_id = speaker.id
    else:
        link.speaker_id = None

    link.source = "manual"
    link.confirmed = payload.confirmed
    link.confirmed_by_user_id = current_user.id if payload.confirmed else None
    db.flush()

    # v3.35.0 refinement: a confirmed assign HARVESTS a voice sample from this
    # session (pooled from the diarizer's fine turns for this cluster), so
    # every naming improves the running centroid — including new acoustic
    # conditions (phone-band vs desk). Previously only the enroll path added
    # samples; plain assigns taught the system nothing. Best-effort.
    fold_status: Optional[str] = None
    if payload.speaker_id and payload.confirmed:
        try:
            from services.speaker_service import add_voice_sample, pooled_embedding

            diarized_doc = (
                session.transcript_diarized
                if isinstance(session.transcript_diarized, dict)
                else {}
            )
            turns = [
                t for t in (diarized_doc.get("speaker_turns") or [])
                if (t.get("speaker") or "").strip() == link.raw_label
            ]
            emb = pooled_embedding(turns) or pooled_embedding(
                _segments_for_label(session, link.raw_label)
            )
            if emb and (speaker.embedding_dim in (None, len(emb))):
                _vs = add_voice_sample(
                    db, speaker, emb,
                    source="assign_confirm",
                    embedding_model=speaker.embedding_model
                    or "pyannote/wespeaker-voxceleb-resnet34-LM",
                    source_session_id=session.id,
                )
                if _vs is not None:
                    logger.info(
                        "assign_confirm: harvested voice sample speaker=%s session=%s "
                        "(samples now %s)", speaker.id, session.id, speaker.sample_count,
                    )
                else:
                    logger.info(
                        "assign_confirm: voice sample NOT folded (inconsistent/mixed "
                        "cluster) speaker=%s session=%s — link still confirmed, "
                        "voiceprint protected", speaker.id, session.id,
                    )
            # v3.36.0 "My Voice": the same pooled embedding also feeds the
            # caller's portable account voiceprint when this speaker is them.
            fold_status = _maybe_fold_my_voice(
                db, current_user, speaker, emb, is_me=payload.is_me
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "assign_confirm sample harvest failed (non-fatal) session=%s: %s",
                session.id, exc,
            )

    # Rewrite the diarized transcript so the SessionDetails page picks up
    # the new label without an extra API roundtrip.
    diarized = session.transcript_diarized or {}
    if isinstance(diarized, dict):
        new_name: Optional[str] = None
        if link.speaker_id:
            sp = db.query(SpeakerProfile).filter(SpeakerProfile.id == link.speaker_id).first()
            if sp:
                new_name = sp.display_name
        new_segments = []
        for seg in diarized.get("segments", []):
            label_now = seg.get("speaker", "")
            raw = seg.get("raw_label", label_now)
            if raw == link.raw_label:
                new_seg = {**seg, "raw_label": raw, "speaker": new_name or raw}
            else:
                new_seg = seg
            new_segments.append(new_seg)
        diarized["segments"] = new_segments
        if "speakers" in diarized:
            seen, ordered = set(), []
            for seg in new_segments:
                s = seg.get("speaker", "Unknown")
                if s not in seen:
                    seen.add(s)
                    ordered.append(s)
            diarized["speakers"] = ordered
        session.transcript_diarized = diarized
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(session, "transcript_diarized")

    # If we just assigned a speaker, propagate the rename into summary text.
    if link.speaker_id:
        sp = db.query(SpeakerProfile).filter(SpeakerProfile.id == link.speaker_id).first()
        if sp:
            _propagate_speaker_rename(session, [link.raw_label], sp.display_name)

    db.commit()
    db.refresh(link)
    await _enqueue_summary_refresh(session, db)
    return _link_to_response(
        db, link, my_voice_folded=_fold_status_to_flag(fold_status)
    )


class CreateSpeakerForLinkRequest(BaseModel):
    """Inline create-and-link payload for the non-admin tagger.

    Just a name (plus optional light contact fields). Unlike /enroll this
    does NOT require any usable audio turn — it creates a name-only
    SpeakerProfile and binds it to an unassigned speaker_link. Used when a
    cluster has no clean audio (short utterances / transcript-only imports)
    so /enroll's 'no usable turn' path would otherwise dead-end the user.
    """
    display_name: str = Field(..., min_length=1, max_length=200)
    email: Optional[str] = Field(default=None, max_length=320)
    title: Optional[str] = Field(default=None, max_length=200)
    company: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=1000)
    # v3.36.0 "My Voice": see SpeakerLinkUpdate.is_me.
    is_me: bool = False


@router.post(
    "/api/sessions/{session_id}/speaker-links/{link_id}/create-speaker",
    response_model=SpeakerLinkResponse,
)
async def create_speaker_for_link(
    session_id: str,
    link_id: int,
    payload: CreateSpeakerForLinkRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Create a NEW (name-only) speaker in the caller's org and link it to an
    unassigned speaker_link on this session.

    Accessible to ANY org member (same inline-tagger surface as PATCH
    /speaker-links/{id} and POST /speaker-links/{id}/enroll). It deliberately
    does NOT require admin and does NOT open org-wide speaker management:
    it can only create a brand-new profile in the caller's own org and bind
    it to a link they can already see. It never edits/deletes existing
    speakers and never crosses orgs.

    Differs from /enroll in that it needs no audio turn / embedding — so it
    works for transcript-only sessions and short clusters. If a speaker with
    the same display_name already exists in this org it is reused (no
    duplicate, no 409/500 from the org+name unique constraint).
    """
    # v3.18.3: speaker_library write gate (creates+links persistent library state).
    gate_feature_for_caller(current_user, "speaker_library", active_org)

    session = _get_session_or_404(
        db,
        active_org.organization.id,
        session_id,
        current_user,
        min_level="edit",
        require_speaker_editor=True,
    )
    link = (
        db.query(SpeakerSessionLink)
        .filter(
            SpeakerSessionLink.id == link_id,
            SpeakerSessionLink.session_id == session.id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Speaker link not found")
    # Only fill an UNASSIGNED cluster, so we never silently relabel a cluster
    # someone already tagged. To re-point a tagged link, clear it via PATCH first.
    if link.speaker_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This speaker label is already linked. Clear it before creating a new speaker.",
        )

    # Find-or-reuse within the SESSION's org (org+display_name is unique;
    # reuse avoids a DB IntegrityError and matches /enroll's behaviour).
    # v3.34.0: keyed off session.organization_id, NOT the caller's active
    # workspace — a superuser naming a speaker on another workspace's
    # meeting (god-view) was silently enrolling the voiceprint into their
    # OWN workspace, where this session's identify can never see it.
    speaker = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.organization_id == session.organization_id,
            SpeakerProfile.display_name == payload.display_name,
        )
        .first()
    )
    if speaker is None:
        speaker = SpeakerProfile(
            organization_id=session.organization_id,
            display_name=payload.display_name,
            email=payload.email or None,
            title=payload.title or None,
            company=payload.company or None,
            notes=payload.notes or None,
            created_by_user_id=current_user.id,
        )
        db.add(speaker)
        db.flush()

    if payload.is_me:
        # v3.36.0 "My Voice": bind the (new or reused) profile to the caller.
        # 409 when a reused same-name profile belongs to a different user.
        _apply_is_me_claim(speaker, current_user)

    # Link + confirm (user-asserted match).
    link.speaker_id = speaker.id
    link.source = "manual"
    link.confirmed = True
    link.confirmed_by_user_id = current_user.id

    # v3.36.0 "My Voice": when the caller asserts/implies this cluster is
    # themself, pool an embedding from this session's turn bank (same shape
    # as the assign-confirm harvest) and fold it into their portable
    # user_voice_profile. Best-effort — never fails the naming.
    fold_status: Optional[str] = None
    try:
        from services.speaker_service import pooled_embedding

        diarized_doc = (
            session.transcript_diarized
            if isinstance(session.transcript_diarized, dict)
            else {}
        )
        my_voice_turns = [
            t for t in (diarized_doc.get("speaker_turns") or [])
            if (t.get("speaker") or "").strip() == link.raw_label
        ]
        my_voice_emb = pooled_embedding(my_voice_turns) or pooled_embedding(
            _segments_for_label(session, link.raw_label)
        )
        fold_status = _maybe_fold_my_voice(
            db, current_user, speaker, my_voice_emb, is_me=payload.is_me
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "create_speaker_for_link my-voice harvest failed (non-fatal) session=%s: %s",
            session.id, exc,
        )

    # Rewrite transcript_diarized.segments so the SessionDetails view shows
    # the new name everywhere this raw_label appears (mirrors /enroll step 6).
    diarized = session.transcript_diarized if isinstance(session.transcript_diarized, dict) else {}
    if isinstance(diarized, dict):
        for seg in diarized.get("segments", []):
            raw = seg.get("raw_label") or seg.get("speaker") or ""
            if raw == link.raw_label:
                seg["raw_label"] = raw
                seg["speaker"] = speaker.display_name
        if "speakers" in diarized:
            seen, ordered = set(), []
            for seg in diarized.get("segments", []):
                s = seg.get("speaker", "Unknown")
                if s not in seen:
                    seen.add(s)
                    ordered.append(s)
            diarized["speakers"] = ordered
        session.transcript_diarized = diarized
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(session, "transcript_diarized")

    # Update Transcription rows carrying this raw_label (mirrors /enroll step 7).
    rows = (
        db.query(Transcription)
        .filter(
            Transcription.session_id == session.id,
            Transcription.speaker == link.raw_label,
        )
        .all()
    )
    for row in rows:
        row.speaker = speaker.display_name

    _propagate_speaker_rename(session, [link.raw_label], speaker.display_name)
    db.commit()
    db.refresh(link)
    logger.info(
        "Inline-created speaker '%s' (id=%d) for session %s cluster %s (org %s, user %s)",
        speaker.display_name, speaker.id, session.id, link.raw_label,
        active_org.organization.id, current_user.id,
    )
    await _enqueue_summary_refresh(session, db)
    return _link_to_response(
        db, link, my_voice_folded=_fold_status_to_flag(fold_status)
    )


class RediarizeRequest(BaseModel):
    num_speakers: Optional[int] = None
    min_speakers: Optional[int] = None
    max_speakers: Optional[int] = None
    clustering_threshold: Optional[float] = None


class RediarizeAcceptedResponse(BaseModel):
    status: str  # "queued"
    session_id: str


class RediarizeStatusResponse(BaseModel):
    status: str  # queued | running | done | failed
    segments: Optional[int] = None
    unique_speakers: Optional[int] = None
    linked: Optional[int] = None
    unmatched: Optional[int] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


async def _run_rediarize_task(
    session_pk: int,
    org_id: int,
    request: "RediarizeRequest",
) -> None:
    """Background worker for rediarize. Uses its own DB session because
    the request-scoped session is closed by the time this runs."""
    import os
    from datetime import datetime, timezone
    from sqlalchemy.orm.attributes import flag_modified
    from database.database import SessionLocal
    from database.models import RecordingSession
    from services.providers.registry import get_provider_registry
    from api.uploads import _assign_speakers_from_diarization

    db = SessionLocal()
    session = None
    try:
        # Look up by pk only: the endpoint already resolved the session
        # (including the cross-org collaborator/personal-org fallback) and
        # authorized the caller. Re-filtering by the caller's ACTIVE org id
        # here used to lose cross-org sessions — the task bailed "not
        # found" and the UI polled a permanently-"queued" job.
        session = db.query(RecordingSession).filter(
            RecordingSession.id == session_pk,
        ).first()
        if not session:
            logger.warning("rediarize_task: session %s not found", session_pk)
            return

        metadata = session.processing_metadata or {}
        metadata["rediarize_status"] = "running"
        metadata["rediarize_started_at"] = datetime.now(timezone.utc).isoformat()
        metadata.pop("rediarize_error", None)
        session.processing_metadata = metadata
        flag_modified(session, "processing_metadata")
        db.commit()

        if not session.audio_file or not os.path.exists(session.audio_file):
            raise RuntimeError(f"audio_file missing: {session.audio_file}")

        registry = get_provider_registry(db)
        diar_provider = registry.get_diarization(session.organization_id)
        if request.clustering_threshold is not None:
            # speaker-svc rejects per-request clustering_threshold overrides
            # (env-var-only since its task #79 v2 — pyannote VRAM churn).
            # Drop it instead of letting the whole re-detect 400; stale
            # cached frontends still send it in Auto mode.
            logger.info(
                "rediarize_task: dropping unsupported clustering_threshold override for session %s",
                session_pk,
            )
            request.clustering_threshold = None
        diar_segments = await diar_provider.diarize(
            session.audio_file,
            num_speakers=request.num_speakers,
            min_speakers=request.min_speakers,
            max_speakers=request.max_speakers,
            clustering_threshold=request.clustering_threshold,
        )
        if not diar_segments:
            raise RuntimeError("diarization returned no segments")

        _assign_speakers_from_diarization(session, diar_segments)
        db.query(SpeakerSessionLink).filter(SpeakerSessionLink.session_id == session.id).delete()
        db.commit()

        summary = identify_speakers(session, db)
        session.ai_insights = None  # force re-extract on next load
        db.commit()

        diarized = session.transcript_diarized if isinstance(session.transcript_diarized, dict) else {}
        segs = diarized.get("segments", []) if isinstance(diarized, dict) else []
        unique = len({(s.get("raw_label") or s.get("speaker") or "") for s in segs if s.get("speaker")})

        metadata = session.processing_metadata or {}
        metadata["rediarize_status"] = "done"
        metadata["rediarize_finished_at"] = datetime.now(timezone.utc).isoformat()
        metadata["rediarize_result"] = {
            "segments": len(segs),
            "unique_speakers": unique,
            "linked": summary.get("linked", 0),
            "unmatched": summary.get("unmatched", 0),
        }
        session.processing_metadata = metadata
        flag_modified(session, "processing_metadata")
        db.commit()
        logger.info("rediarize_task done for session %s: %d unique speakers", session.id, unique)
        # Re-diarization rewrote the whole transcript (new clusters + re-matched
        # names), so the existing summary references stale labels. Regenerate it
        # from the fresh, named transcript. Best-effort — the helper never
        # raises, and a regen failure must not fail the rediarize.
        await _enqueue_summary_refresh(session, db)
    except Exception as exc:  # noqa: BLE001
        logger.exception("rediarize_task failed for session %s", session_pk)
        try:
            if session is not None:
                metadata = session.processing_metadata or {}
                metadata["rediarize_status"] = "failed"
                metadata["rediarize_error"] = str(exc)[:500]
                metadata["rediarize_finished_at"] = datetime.now(timezone.utc).isoformat()
                session.processing_metadata = metadata
                flag_modified(session, "processing_metadata")
                db.commit()
        except Exception:
            db.rollback()
    finally:
        db.close()


@router.post(
    "/api/sessions/{session_id}/rediarize",
    response_model=RediarizeAcceptedResponse,
    status_code=202,
)
async def rediarize_session_endpoint(
    session_id: str,
    request: RediarizeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Kick off async re-diarization. Returns 202 immediately so the
    request doesn't trip Cloudflare's 100s timeout on long recordings.
    Frontend polls /rediarize-status to know when it's done.
    """
    import asyncio
    import os
    from datetime import datetime, timezone
    from sqlalchemy.orm.attributes import flag_modified

    session = _get_session_or_404(
        db,
        active_org.organization.id,
        session_id,
        current_user,
        min_level="edit",
        require_speaker_editor=True,
    )
    if not session.audio_file or not os.path.exists(session.audio_file):
        raise HTTPException(status_code=400, detail="Session has no audio file to re-diarize")

    # Refuse if one's already running on this session, otherwise we'd
    # race two background tasks writing the same row.
    metadata = session.processing_metadata or {}
    if metadata.get("rediarize_status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="A re-diarize is already in progress for this session")

    metadata["rediarize_status"] = "queued"
    metadata["rediarize_queued_at"] = datetime.now(timezone.utc).isoformat()
    metadata.pop("rediarize_error", None)
    metadata.pop("rediarize_result", None)
    session.processing_metadata = metadata
    flag_modified(session, "processing_metadata")
    db.commit()

    asyncio.create_task(
        # Pass the SESSION's org, not the caller's active org — they differ
        # for cross-org sessions (per-meeting collaborators, personal-org
        # recordings viewed from another workspace).
        _run_rediarize_task(session.id, session.organization_id, request)
    )

    return RediarizeAcceptedResponse(status="queued", session_id=str(session.session_id or session.id))


@router.get(
    "/api/sessions/{session_id}/rediarize-status",
    response_model=RediarizeStatusResponse,
)
async def rediarize_status_endpoint(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Poll the rediarize job state for this session."""
    session = _get_session_or_404(db, active_org.organization.id, session_id, current_user)
    metadata = session.processing_metadata or {}
    result = metadata.get("rediarize_result") or {}
    return RediarizeStatusResponse(
        status=metadata.get("rediarize_status", "idle"),
        segments=result.get("segments"),
        unique_speakers=result.get("unique_speakers"),
        linked=result.get("linked"),
        unmatched=result.get("unmatched"),
        error=metadata.get("rediarize_error"),
        started_at=metadata.get("rediarize_started_at"),
        finished_at=metadata.get("rediarize_finished_at"),
    )


# Note: the old synchronous behaviour is gone — Cloudflare's 100s window
# made it impossible to keep on long recordings. Anything calling this
# endpoint must now expect 202 and poll the status endpoint.


# ---- legacy stub removed; the function body below is the historical
# ---- placeholder so the file diff stays small.
async def _DEAD_OLD_REDIARIZE_FALLBACK_DO_NOT_CALL():
    diarized = None  # noqa: F841
    segs = []  # noqa: F841
    unique = 0  # noqa: F841

    return None  # type: ignore[return-value]  # placeholder


class EnrollFromSessionRequest(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=120)
    email: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=1000)
    # v3.36.0 "My Voice": see SpeakerLinkUpdate.is_me.
    is_me: bool = False



def _propagate_speaker_rename(
    session,
    old_labels,
    new_label: str,
) -> bool:
    """Rewrite every occurrence of any string in old_labels (word-bounded)
    to new_label across the LLM-generated summary fields on the session.

    Called after enroll-from-session, assign-to-existing, and merge so the
    AI Summary text (executive / bullets / actions / decisions / quotes /
    next_steps / minutes) plus the legacy summary JSON column plus the
    ai_insights side panel all show the named speaker instead of the
    raw SPEAKER_XX cluster id. Caller is responsible for db.commit().
    Returns True if anything changed.
    """
    import json
    import re
    from sqlalchemy.orm.attributes import flag_modified

    if not old_labels or not new_label:
        return False

    olds = sorted(
        {label for label in old_labels if label and label != new_label},
        key=len,
        reverse=True,
    )
    if not olds:
        return False

    pattern = re.compile(
        r"\b(" + "|".join(re.escape(label) for label in olds) + r")\b"
    )

    def _walk(val):
        if isinstance(val, str):
            return pattern.sub(new_label, val)
        if isinstance(val, list):
            return [_walk(x) for x in val]
        if isinstance(val, dict):
            return {k: _walk(v) for k, v in val.items()}
        return val

    changed = False

    fs = session.final_summary
    if isinstance(fs, dict):
        new_fs = _walk(fs)
        if new_fs != fs:
            session.final_summary = new_fs
            flag_modified(session, "final_summary")
            changed = True

    summary = session.summary
    if isinstance(summary, str) and summary.strip():
        try:
            data = json.loads(summary)
            new_data = _walk(data)
            if new_data != data:
                session.summary = json.dumps(new_data)
                changed = True
        except json.JSONDecodeError:
            new_text = pattern.sub(new_label, summary)
            if new_text != summary:
                session.summary = new_text
                changed = True

    insights = session.ai_insights
    if isinstance(insights, dict):
        new_ins = _walk(insights)
        if new_ins != insights:
            session.ai_insights = new_ins
            flag_modified(session, "ai_insights")
            changed = True

    return changed


def apply_rename_to_history(db: Session, speaker: SpeakerProfile, old_name: str) -> int:
    """Persistent-identity (v3.43): after a profile is renamed, fix EVERY past
    meeting it appears in.

    v3.44 fully-dynamic transcripts: the diarized transcript is NO LONGER
    rewritten here — every display surface renders the speaker NAME live from
    the current profile (hydrate_diarized_*), so a rename shows everywhere
    instantly without baking a name into stored segments. This function now
    only fixes the SUMMARY / insights free text, which is LLM-generated prose
    that can't be rendered live and so must be string-replaced in place.
    Cheap (string ops, no LLM, no re-STT). Returns the number of sessions
    changed. The heavier LLM re-summary is the separate /resummarize-history."""
    new_name = speaker.display_name
    if not new_name or new_name == old_name:
        return 0
    links = (
        db.query(SpeakerSessionLink)
        .filter(SpeakerSessionLink.speaker_id == speaker.id)
        .all()
    )
    labels_by_session: dict[int, set] = {}
    for link in links:
        labels_by_session.setdefault(link.session_id, set()).add((link.raw_label or "").strip())

    affected = 0
    for session_id, raw_labels in labels_by_session.items():
        session = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.id == session_id,
                RecordingSession.organization_id == speaker.organization_id,
            )
            .first()
        )
        if session is None:
            continue
        # Summary/insights string-replace ONLY: old handle/name + raw labels ->
        # new name. Transcripts render live (see docstring), so we don't touch
        # transcript_diarized here anymore.
        old_labels = [lbl for lbl in raw_labels if lbl] + ([old_name] if old_name else [])
        if _propagate_speaker_rename(session, old_labels, new_name):
            affected += 1
    if affected:
        db.commit()
    logger.info("apply_rename_to_history: speaker=%s %r->%r fixed summaries in %d past meeting(s)",
                speaker.id, old_name, new_name, affected)
    return affected


def _propagate_rename_bg(speaker_id: int, org_id: int, old_name: str) -> None:
    """Background task: re-label a renamed speaker's past meetings OFF the request
    path so the rename returns instantly even for a speaker in many meetings. The
    session-detail view renders names live (hydrate_diarized_speaker_names), so the
    UI is correct immediately; this catches up the stored transcripts + summaries
    for exports/chat/index. Opens its own DB session (request session is closed)."""
    from database.database import SessionLocal
    db = SessionLocal()
    try:
        sp = (
            db.query(SpeakerProfile)
            .filter(
                SpeakerProfile.id == speaker_id,
                SpeakerProfile.organization_id == org_id,
            )
            .first()
        )
        if sp is not None:
            apply_rename_to_history(db, sp, old_name)
    except Exception:  # noqa: BLE001
        logger.exception("background rename propagation failed speaker=%s", speaker_id)
    finally:
        db.close()


@router.post(
    "/api/sessions/{session_id}/speaker-links/{link_id}/enroll",
    response_model=SpeakerLinkResponse,
)
async def enroll_speaker_from_session(
    session_id: str,
    link_id: int,
    payload: EnrollFromSessionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """One-click enrollment: take the user-supplied name + the longest
    turn of this cluster's audio, create (or reuse) a SpeakerProfile,
    save a voice sample (ECAPA embedding), and confirm the speaker_link.
    """
    # v3.18.3: speaker_library write gate.
    gate_feature_for_caller(current_user, "speaker_library", active_org)

    import os
    import subprocess
    import tempfile

    session = _get_session_or_404(
        db,
        active_org.organization.id,
        session_id,
        current_user,
        min_level="edit",
        require_speaker_editor=True,
    )
    link = (
        db.query(SpeakerSessionLink)
        .filter(
            SpeakerSessionLink.id == link_id,
            SpeakerSessionLink.session_id == session.id,
        )
        .first()
    )
    if not link:
        raise HTTPException(status_code=404, detail="Speaker link not found")

    if not session.audio_file or not os.path.exists(session.audio_file):
        raise HTTPException(status_code=400, detail="Session audio file is missing")

    # 1) Find the longest turn for this raw_label in transcript_diarized.
    diarized = session.transcript_diarized if isinstance(session.transcript_diarized, dict) else {}
    segs = diarized.get("segments", []) if isinstance(diarized, dict) else []
    longest = None
    longest_duration = 0.0
    for seg in segs:
        raw = seg.get("raw_label") or seg.get("speaker") or ""
        if raw != link.raw_label:
            continue
        try:
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or 0)
        except (TypeError, ValueError):
            continue
        dur = end - start
        if dur > longest_duration and dur >= 1.0:
            longest_duration = dur
            longest = (start, end)
    if longest is None:
        raise HTTPException(
            status_code=400,
            detail=f"No usable turn (>=1s) for {link.raw_label} found in this session",
        )

    # 2) Slice that turn into a temp wav. Cap at 30s so embedding stays
    # fast and the sample is a representative chunk.
    seg_start, seg_end = longest
    if longest_duration > 30.0:
        seg_end = seg_start + 30.0
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        slice_path = tmp.name
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{seg_start:.3f}",
                "-i", session.audio_file,
                "-t", f"{(seg_end - seg_start):.3f}",
                "-ac", "1", "-ar", "16000",
                slice_path,
            ],
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="ignore")[:300]
            raise HTTPException(
                status_code=500,
                detail=f"ffmpeg slice failed: {err}",
            )
        with open(slice_path, "rb") as fh:
            audio_bytes = fh.read()
    finally:
        try:
            os.remove(slice_path)
        except OSError:
            pass

    # 3) Find or create the SpeakerProfile.
    # Session-scoped enrollment: the voiceprint belongs to the workspace the
    # MEETING lives in (session.organization_id), not the caller's active
    # workspace — see the create_speaker_for_link comment (v3.34.0).
    speaker = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.organization_id == session.organization_id,
            SpeakerProfile.display_name == payload.display_name,
        )
        .first()
    )
    created_new = False
    if not speaker:
        speaker = SpeakerProfile(
            organization_id=session.organization_id,
            display_name=payload.display_name,
            email=payload.email,
            notes=payload.notes,
            created_by_user_id=current_user.id,
        )
        db.add(speaker)
        db.flush()
        created_new = True

    if payload.is_me:
        # v3.36.0 "My Voice": bind the (new or reused) profile to the caller.
        # 409 when a reused same-name profile belongs to a different user —
        # raised BEFORE any link mutation / sample write (mirrors
        # create_speaker_for_link).
        _apply_is_me_claim(speaker, current_user)

    # 4) Embed the slice via speaker-svc and add as a voice sample.
    registry = get_provider_registry(db)
    provider = registry.get_speaker_svc(active_org.organization.id)
    embed_result = await provider.embed_bytes(audio_bytes, filename=f"sess{session.id}_{link.raw_label}.wav")
    embedding = embed_result.get("embedding") or []
    if not embedding:
        if created_new:
            db.delete(speaker)
            db.commit()
        raise HTTPException(status_code=502, detail="speaker-svc returned no embedding")

    embedding_model = embed_result.get("model") or DEFAULT_EMBEDDING_MODEL
    duration = embed_result.get("duration_seconds") or (seg_end - seg_start)

    audio_path: Optional[str] = None
    if STORE_SPEAKER_AUDIO:
        os.makedirs(VOICE_SAMPLE_DIR, exist_ok=True)
        audio_path = os.path.join(
            VOICE_SAMPLE_DIR,
            f"speaker_{speaker.id}_sess{session.id}_{os.urandom(4).hex()}.wav",
        )
        try:
            with open(audio_path, "wb") as fh:
                fh.write(audio_bytes)
        except OSError as exc:
            logger.warning("Failed to persist enrollment audio: %s", exc)
            audio_path = None

    add_voice_sample(
        db,
        speaker,
        embedding,
        source="session",
        embedding_model=embedding_model,
        audio_path=audio_path,
        duration_seconds=duration,
        source_session_id=session.id,
    )

    # v3.36.0 "My Voice": fold the freshly-embedded sample into the caller's
    # portable user_voice_profile when this cluster is asserted (is_me) or
    # inferred to be the caller. Best-effort — never fails the enrollment.
    fold_status = _maybe_fold_my_voice(
        db, current_user, speaker, list(embedding), is_me=payload.is_me
    )

    # 5) Confirm the speaker_session_link.
    link.speaker_id = speaker.id
    link.source = "manual"
    link.confirmed = True
    link.confirmed_by_user_id = current_user.id
    link.similarity = 1.0  # user-asserted match

    # 6) Rewrite transcript_diarized.segments so the speaker label is
    # the display_name everywhere this raw_label appears.
    if isinstance(diarized, dict):
        for seg in diarized.get("segments", []):
            raw = seg.get("raw_label") or seg.get("speaker") or ""
            if raw == link.raw_label:
                seg["raw_label"] = raw
                seg["speaker"] = speaker.display_name
        if "speakers" in diarized:
            seen, ordered = set(), []
            for seg in diarized.get("segments", []):
                s = seg.get("speaker", "Unknown")
                if s not in seen:
                    seen.add(s)
                    ordered.append(s)
            diarized["speakers"] = ordered
        session.transcript_diarized = diarized
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(session, "transcript_diarized")

    # 7) Also update Transcription rows with this raw_label so the list
    # view picks up the display name.
    rows = (
        db.query(Transcription)
        .filter(
            Transcription.session_id == session.id,
            Transcription.speaker == link.raw_label,
        )
        .all()
    )
    for row in rows:
        row.speaker = speaker.display_name

    _propagate_speaker_rename(session, [link.raw_label], speaker.display_name)
    db.commit()
    db.refresh(link)
    logger.info(
        "Enrolled speaker '%s' (id=%d, new=%s) from session %s cluster %s (turn %.1fs-%.1fs)",
        speaker.display_name, speaker.id, created_new, session.id, link.raw_label, seg_start, seg_end,
    )
    await _enqueue_summary_refresh(session, db)
    return _link_to_response(
        db, link, my_voice_folded=_fold_status_to_flag(fold_status)
    )


class MergeLinksRequest(BaseModel):
    source_link_id: int
    target_link_id: int


@router.post(
    "/api/sessions/{session_id}/speaker-links/merge",
    response_model=list[SpeakerLinkResponse],
)
async def merge_speaker_links(
    session_id: str,
    payload: MergeLinksRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Merge two clusters in this session: source -> target. Useful when
    pyannote over-splits a single voice into multiple SPEAKER_XX
    clusters. The target's identity (speaker_id, display name) is
    preserved; source's segments and Transcription rows are relabeled
    to target. Source link row is deleted.
    """
    # v3.18.3: speaker_library write gate.
    gate_feature_for_caller(current_user, "speaker_library", active_org)

    from sqlalchemy.orm.attributes import flag_modified

    if payload.source_link_id == payload.target_link_id:
        raise HTTPException(status_code=400, detail="source and target must differ")

    session = _get_session_or_404(
        db,
        active_org.organization.id,
        session_id,
        current_user,
        min_level="edit",
        require_speaker_editor=True,
    )
    source = (
        db.query(SpeakerSessionLink)
        .filter(
            SpeakerSessionLink.id == payload.source_link_id,
            SpeakerSessionLink.session_id == session.id,
        )
        .first()
    )
    target = (
        db.query(SpeakerSessionLink)
        .filter(
            SpeakerSessionLink.id == payload.target_link_id,
            SpeakerSessionLink.session_id == session.id,
        )
        .first()
    )
    if not source or not target:
        raise HTTPException(status_code=404, detail="One or both links not found")

    # Resolve target's display name: prefer named SpeakerProfile, else
    # the raw_label as a fallback.
    target_display = target.raw_label
    if target.speaker_id:
        sp = db.query(SpeakerProfile).filter(SpeakerProfile.id == target.speaker_id).first()
        if sp:
            target_display = sp.display_name

    # Track what the SOURCE was displaying so we can rewrite Transcription
    # rows that may have been re-labeled by enrollment already.
    source_display = source.raw_label
    if source.speaker_id:
        sp = db.query(SpeakerProfile).filter(SpeakerProfile.id == source.speaker_id).first()
        if sp:
            source_display = sp.display_name

    # 1) Rewrite transcript_diarized.segments
    diarized = session.transcript_diarized if isinstance(session.transcript_diarized, dict) else {}
    if isinstance(diarized, dict):
        for seg in diarized.get("segments", []):
            raw = seg.get("raw_label") or seg.get("speaker") or ""
            cur = seg.get("speaker") or ""
            if raw == source.raw_label or cur == source.raw_label or cur == source_display:
                seg["raw_label"] = target.raw_label
                seg["speaker"] = target_display
        # Refresh speakers list.
        if "speakers" in diarized:
            seen, ordered = set(), []
            for seg in diarized.get("segments", []):
                s = seg.get("speaker", "Unknown")
                if s not in seen:
                    seen.add(s)
                    ordered.append(s)
            diarized["speakers"] = ordered
        session.transcript_diarized = diarized
        flag_modified(session, "transcript_diarized")

    # 2) Rewrite Transcription rows for the source's labels.
    rows = (
        db.query(Transcription)
        .filter(
            Transcription.session_id == session.id,
            Transcription.speaker.in_([source.raw_label, source_display]),
        )
        .all()
    )
    for row in rows:
        row.speaker = target_display

    # 3) Drop the source link.
    db.delete(source)

    # 4) Propagate source -> target labels in the summary text fields so
    # the AI Summary stops referencing the merged-away SPEAKER_XX.
    olds = [source.raw_label]
    if source_display and source_display != source.raw_label:
        olds.append(source_display)
    _propagate_speaker_rename(session, olds, target_display)

    # 5) Clear ai_insights so the next view re-extracts with the new
    # collapsed speaker list (speaker_insights counts will differ).
    session.ai_insights = None

    db.commit()

    # Return the updated link list (just the target + any other links).
    remaining = (
        db.query(SpeakerSessionLink)
        .filter(SpeakerSessionLink.session_id == session.id)
        .order_by(SpeakerSessionLink.raw_label.asc())
        .all()
    )
    logger.info(
        "Merged speaker link %s into %s for session %s (%d segments + %d rows updated)",
        source.raw_label, target.raw_label, session.id, len(diarized.get("segments", []) or []), len(rows),
    )
    # The merge rewrote this session's transcript labels — regenerate the
    # summary so it stops referencing the merged-away cluster. Best-effort.
    await _enqueue_summary_refresh(session, db)
    return [_link_to_response(db, link) for link in remaining]


@router.post("/api/sessions/{session_id}/identify-speakers", response_model=IdentifyResponse)
async def identify_speakers_endpoint(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Re-run speaker identification on a finished session. Useful after
    enrolling new speakers or to refresh stale mappings."""
    # v3.18.3: speaker_library write gate (mutates speaker_links on the session).
    gate_feature_for_caller(current_user, "speaker_library", active_org)
    session = _get_session_or_404(
        db,
        active_org.organization.id,
        session_id,
        current_user,
        min_level="edit",
        require_speaker_editor=True,
    )
    summary = identify_speakers(session, db)
    # identify_speakers rewrote transcript_diarized speaker names for any
    # newly-matched clusters; regenerate the summary so it picks up the new
    # names instead of the stale "Speaker N". Only when something linked —
    # the summary-input hash would no-op an unchanged transcript anyway, but
    # this avoids enqueuing a job that can't change anything. Best-effort.
    if summary.get("linked"):
        await _enqueue_summary_refresh(session, db)
    return IdentifyResponse(**summary)
