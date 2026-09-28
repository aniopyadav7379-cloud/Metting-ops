"""Durable, tenant-scoped transcript ingestion from Unicorn Stable."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from auth.config import auth_config
from auth.models import Organization, PersonalAccessToken, User, UserOrganization
from auth.organization import ActiveOrganization, normalize_org_slug
from auth.pat import TOKEN_PREFIX as PAT_PREFIX, resolve_pat_record
from auth.utils import check_permission
from database.database import get_db
from database.models import RecordingSession, Transcription

router = APIRouter(prefix="/api/v1/sessions", tags=["stable-ingest"])

# Must remain one of the values accepted by the PostgreSQL
# ck_recording_sessions_mode constraint. Stable provenance is carried by
# source_type/external_source rather than inventing a new lifecycle mode.
STABLE_INGEST_SESSION_MODE = "upload"


class StableTranscriptLine(BaseModel):
    participant_id: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=256)
    text: str = Field(min_length=1, max_length=8000)
    ts: int = Field(ge=0, description="Unix epoch milliseconds")
    is_final: bool = True
    segment_id: Optional[str] = Field(default=None, max_length=256)
    source: Literal["local_mic", "agent_stt", "agent", "unknown"] = "unknown"
    identity_verification: Literal[
        "stable_room_member", "stable_known_agent"
    ] = "stable_room_member"
    identity_verified: Literal[True] = True
    content_verification: Literal[
        "client_caption",
        "agent_stt",
        "agent_generated_caption",
        "unverified",
    ] = "unverified"


class StableTranscriptIngest(BaseModel):
    source: str = Field(pattern=r"^unicorn-stable$")
    export_id: str = Field(min_length=1, max_length=128)
    exported_at: datetime
    room_id: str = Field(min_length=1, max_length=128)
    room_name: str = Field(min_length=1, max_length=256)
    livekit_room_name: Optional[str] = Field(default=None, max_length=256)
    call_started_at: Optional[datetime] = None
    call_ended_at: Optional[datetime] = None
    label: Optional[str] = Field(default=None, max_length=256)
    meeting_ops_session_id: Optional[str] = Field(default=None, max_length=128)
    exporter: dict[str, Any] = Field(default_factory=dict)
    line_count: int = Field(ge=0, le=2000)
    lines: list[StableTranscriptLine] = Field(min_length=1, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StableTranscriptIngestResponse(BaseModel):
    ok: bool = True
    created: bool
    session_id: str
    session_url: str
    external_id: str
    line_count: int


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _response(row: RecordingSession, *, created: bool) -> StableTranscriptIngestResponse:
    public_id = row.session_id or str(row.id)
    return StableTranscriptIngestResponse(
        created=created,
        session_id=public_id,
        session_url=f"{auth_config.APP_BASE_URL}/sessions/{public_id}",
        external_id=row.external_id or "",
        line_count=len((row.transcript_diarized or {}).get("segments", [])),
    )


def _payload_sha256(body: StableTranscriptIngest) -> str:
    canonical = json.dumps(
        body.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _stored_payload_sha256(row: RecordingSession) -> Optional[str]:
    metadata = row.processing_metadata if isinstance(row.processing_metadata, dict) else {}
    federation = metadata.get("federation")
    if not isinstance(federation, dict):
        return None
    value = federation.get("payload_sha256")
    return value if isinstance(value, str) else None


@dataclass
class StableIngestPrincipal:
    user: User
    token: PersonalAccessToken
    active_org: ActiveOrganization


def require_stable_ingest_principal(
    request: Request,
    db: Session = Depends(get_db),
) -> StableIngestPrincipal:
    """Authenticate one org-bound Stable integration PAT.

    JWTs, browser cookies, forward-auth identity, and legacy API keys are
    intentionally not considered by this machine-to-machine endpoint.
    """
    authorization = request.headers.get("Authorization") or ""
    scheme, _, plaintext = authorization.partition(" ")
    plaintext = plaintext.strip()
    if (
        scheme.lower() != "bearer"
        or not plaintext
        or not plaintext.startswith(PAT_PREFIX)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A Stable integration PAT is required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = resolve_pat_record(db, plaintext=plaintext)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked integration PAT",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if token.scope != "stable.transcript.ingest" or token.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PAT is not authorized for Stable transcript ingestion",
        )

    raw_slug = request.headers.get("X-MeetingOps-Org")
    if not raw_slug or not raw_slug.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-MeetingOps-Org header is required",
        )
    try:
        slug = normalize_org_slug(raw_slug)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-MeetingOps-Org header is invalid",
        )

    organization = (
        db.query(Organization)
        .filter(
            Organization.slug == slug,
            Organization.is_active.is_(True),
        )
        .first()
    )
    if organization is None:
        # Do not reveal inactive organizations or any additional tenant facts.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    if token.organization_id != organization.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PAT is not authorized for this organization",
        )

    current_user = token.user
    membership = (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id == current_user.id,
            UserOrganization.organization_id == organization.id,
        )
        .first()
    )
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized for this organization",
        )

    role = "superuser" if current_user.is_superuser else membership.role
    if not check_permission(role, "session.create"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to create sessions in this organization",
        )
    return StableIngestPrincipal(
        user=current_user,
        token=token,
        active_org=ActiveOrganization(
            organization=organization,
            membership=membership,
            role=role,
        ),
    )


@router.post(
    "/ingest",
    response_model=StableTranscriptIngestResponse,
    status_code=status.HTTP_200_OK,
)
async def ingest_stable_transcript(
    body: StableTranscriptIngest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    principal: StableIngestPrincipal = Depends(require_stable_ingest_principal),
) -> StableTranscriptIngestResponse:
    """Create one completed Meeting-Ops session per Stable export.

    Authentication accepts only an unexpired ``stable.transcript.ingest`` PAT
    bound to the exact organization in ``X-MeetingOps-Org``. Browser identity,
    generic user PATs, JWTs, cookies, and legacy API keys are not accepted.
    """
    if idempotency_key and idempotency_key != body.export_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Idempotency-Key must match export_id",
        )
    if body.line_count != len(body.lines):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="line_count does not match lines",
        )

    current_user = principal.user
    org_id = principal.active_org.organization.id
    payload_sha256 = _payload_sha256(body)
    existing = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.organization_id == org_id,
            RecordingSession.external_source == body.source,
            RecordingSession.external_id == body.export_id,
        )
        .first()
    )
    if existing is not None:
        if _stored_payload_sha256(existing) != payload_sha256:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="export_id was already used for different transcript content",
            )
        return _response(existing, created=False)

    lines = sorted(body.lines, key=lambda line: (line.ts, line.name, line.participant_id))
    first_ms = lines[0].ts
    started = _utc(body.call_started_at) or datetime.fromtimestamp(
        first_ms / 1000, tz=timezone.utc
    )
    ended = _utc(body.call_ended_at) or datetime.fromtimestamp(
        lines[-1].ts / 1000, tz=timezone.utc
    )
    if ended < started:
        ended = started

    speakers: list[str] = []
    participants_by_id: dict[str, dict[str, str]] = {}
    segments: list[dict[str, Any]] = []
    transcript_rows: list[Transcription] = []
    for line in lines:
        speaker = (line.name or line.participant_id).strip()
        if speaker not in speakers:
            speakers.append(speaker)
        participant = participants_by_id.setdefault(
            line.participant_id,
            {
                "id": line.participant_id,
                "name": speaker,
                "role": "agent" if line.source == "agent" else "participant",
                "kind": "agent" if line.source == "agent" else "human",
            },
        )
        if line.source == "agent":
            participant["role"] = "agent"
            participant["kind"] = "agent"
        relative_start = max(0.0, (line.ts - int(started.timestamp() * 1000)) / 1000)
        segment = {
            "start": relative_start,
            "end": relative_start,
            "text": line.text.strip(),
            "speaker": speaker,
            "participant_id": line.participant_id,
            "source": line.source,
            "confidence": None,
            "identity_verification": line.identity_verification,
            "identity_verified": line.identity_verified,
            "content_verification": line.content_verification,
            **({"segment_id": line.segment_id} if line.segment_id else {}),
        }
        segments.append(segment)

    plain_text = "\n\n".join(line.text.strip() for line in lines)
    session_uuid = str(uuid.uuid4())
    provenance = {
        "source": body.source,
        "export_id": body.export_id,
        "exported_at": _utc(body.exported_at).isoformat(),
        "stable_room_id": body.room_id,
        "stable_livekit_room_name": body.livekit_room_name,
        "exporter": body.exporter,
        "metadata": body.metadata,
        "payload_sha256": payload_sha256,
        "ingest_principal": {
            "user_id": current_user.id,
            "pat_id": principal.token.id,
            "scope": principal.token.scope,
            "organization_id": principal.token.organization_id,
        },
        "verification": {
            "speaker_identity": "stable_server_authorized",
            "transcript_text": "unverified",
            "acoustic_confidence": "not_provided",
        },
    }
    row = RecordingSession(
        session_id=session_uuid,
        name=body.label or f"Stable call · {body.room_name}",
        title=body.label or f"Stable call · {body.room_name}",
        description=f"Transcript imported from Unicorn Stable room {body.room_name}.",
        status="completed",
        # Imported transcripts use the existing upload pipeline mode. Stable
        # provenance remains explicit in source_type/external_source; using a
        # new ad-hoc mode would violate ck_recording_sessions_mode in Postgres.
        mode=STABLE_INGEST_SESSION_MODE,
        created_at=_utc(body.exported_at),
        started_at=started,
        ended_at=ended,
        meeting_date=started.date(),
        meeting_time=started.timetz().replace(tzinfo=None),
        duration=max(0.0, (ended - started).total_seconds()),
        transcript=plain_text,
        transcript_simple=plain_text,
        transcript_diarized={
            "text": plain_text,
            "segments": segments,
            "speakers": speakers,
            "source": body.source,
        },
        # Display names are not identities: two people may share one. Tenant
        # analytics and diarization cardinality therefore count stable,
        # server-authorized participant IDs.
        speaker_count=len(participants_by_id),
        user_id=current_user.id,
        organization_id=org_id,
        participants=list(participants_by_id.values()),
        source_type="stable",
        external_source=body.source,
        external_id=body.export_id,
        room_name=body.room_name,
        extra_data={"federation": provenance},
        processing_metadata={"federation": provenance, "processing_complete": True},
    )
    db.add(row)
    try:
        db.flush()
        for segment in segments:
            transcript_rows.append(
                Transcription(
                    session_id=row.id,
                    text=segment["text"],
                    speaker=segment["speaker"],
                    start_time=segment["start"],
                    end_time=segment["end"],
                    confidence=None,
                )
            )
        db.add_all(transcript_rows)
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        existing = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.organization_id == org_id,
                RecordingSession.external_source == body.source,
                RecordingSession.external_id == body.export_id,
            )
            .first()
        )
        if existing is None:
            raise
        if _stored_payload_sha256(existing) != payload_sha256:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="export_id was already used for different transcript content",
            )
        return _response(existing, created=False)

    return _response(row, created=True)
