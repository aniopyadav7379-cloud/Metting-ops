"""Narrow Glitter Mane -> Meeting-Ops medical-visit integration surface.

This router deliberately does NOT reuse the backend's broad auth dependency.
Only a token exchanged for the dedicated medical-visit audience may reach these
endpoints, and only on this router.
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile, status
from jose import jwk, jwt
from jose.exceptions import ExpiredSignatureError, JWKError, JWTClaimsError, JWTError
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from auth.models import User
from auth.organization import (
    ActiveOrganization,
    ORG_HEADER_NAME,
    is_global_admin,
    parse_group_list,
    resolve_active_organization,
)
from auth.service import AuthService
from database.database import get_db
from database.models import RecordingSession as DBRecordingSession
from services import media_storage

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/integrations/medical-visits", tags=["medical-visits"])

_JWKS_TTL_SECONDS = 24 * 60 * 60
_CLOCK_SKEW_SECONDS = 30
_ALGORITHMS = ["RS256"]
_TRANSIENT_KIND = "medical_visit"
_TRANSIENT_TAG = "medical-visit"


def _kc_base() -> str:
    return os.getenv("KEYCLOAK_URL", "https://auth.unicorncommander.ai").rstrip("/")


def _kc_realm() -> str:
    return os.getenv("KEYCLOAK_REALM", "uchub").strip() or "uchub"


def _issuer() -> str:
    return f"{_kc_base()}/realms/{_kc_realm()}"


def _jwks_url() -> str:
    return f"{_issuer()}/protocol/openid-connect/certs"


def _expected_audience() -> str:
    return os.getenv("MEDICAL_VISITS_EXPECTED_AUDIENCE", "meeting-ops-medical-visits").strip()


def _expected_actor() -> str:
    return os.getenv("MEDICAL_VISITS_EXPECTED_AZP", "glitter-mane").strip()


class MedicalVisitCreateRequest(BaseModel):
    source_label: str | None = Field(default=None, max_length=255)
    client_session_key: str | None = Field(default=None, max_length=255)
    correlation_id: str | None = Field(default=None, max_length=255)


class MedicalVisitCaller:
    def __init__(self, user: User, active_org: ActiveOrganization, claims: dict[str, Any]) -> None:
        self.user = user
        self.active_org = active_org
        self.claims = claims


class _KCJWKSCache:
    def __init__(self) -> None:
        self._jwks: dict[str, Any] | None = None
        self._expires_at = 0.0

    def resolve_key(self, kid: str) -> dict[str, Any] | None:
        import time

        now = time.time()
        if self._jwks is None or now >= self._expires_at:
            self.refresh(force=False)
        key = self._find(kid)
        if key is not None:
            return key
        self.refresh(force=True)
        return self._find(kid)

    def refresh(self, *, force: bool) -> None:
        import time

        now = time.time()
        if not force and self._jwks is not None and now < self._expires_at:
            return
        try:
            with httpx.Client(
                timeout=10,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                    )
                },
            ) as client:
                resp = client.get(_jwks_url())
                resp.raise_for_status()
                self._jwks = resp.json()
                self._expires_at = now + _JWKS_TTL_SECONDS
        except Exception as exc:  # noqa: BLE001
            logger.warning("medical_visits: JWKS refresh failed: %s", exc)

    def _find(self, kid: str) -> dict[str, Any] | None:
        keys = (self._jwks or {}).get("keys", [])
        if not isinstance(keys, list):
            return None
        for key in keys:
            if isinstance(key, dict) and key.get("kid") == kid:
                return key
        return None


_jwks_cache = _KCJWKSCache()


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    parts = auth.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    return parts[1].strip()


def _verify_medical_visit_token(token: str) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    kid = header.get("kid")
    alg = header.get("alg")
    if alg not in _ALGORITHMS or not isinstance(kid, str) or not kid:
        raise HTTPException(status_code=401, detail="invalid bearer token")
    key = _jwks_cache.resolve_key(kid)
    if key is None:
        raise HTTPException(status_code=401, detail="token signing key not available")
    try:
        signing_key = jwk.construct(key, algorithm=alg)
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=_ALGORITHMS,
            audience=_expected_audience(),
            issuer=_issuer(),
            options={
                "require_exp": True,
                "require_iat": True,
                "require_nbf": True,
                "require_aud": True,
                "require_iss": True,
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_nbf": True,
                "verify_aud": True,
                "verify_iss": True,
                "leeway": _CLOCK_SKEW_SECONDS,
            },
        )
    except (ExpiredSignatureError, JWTClaimsError, JWTError, JWKError) as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    actor = str(claims.get("azp") or "").strip()
    if actor != _expected_actor():
        raise HTTPException(status_code=403, detail="token actor is not allowed for medical visits")
    return claims


def _token_groups(claims: dict[str, Any]) -> list[str]:
    raw = claims.get("groups") or []
    if isinstance(raw, str):
        raw = [raw]
    return parse_group_list(str(g) for g in raw if g)


def _token_email(claims: dict[str, Any]) -> str:
    for value in (
        claims.get("email"),
        claims.get("preferred_username"),
        claims.get("upn"),
        claims.get("sub"),
    ):
        if isinstance(value, str) and "@" in value:
            return value.strip().lower()
    raise HTTPException(status_code=401, detail="token missing email identity")


def _medical_visit_metadata(session: DBRecordingSession) -> dict[str, Any]:
    meta = dict(session.processing_metadata or {})
    visit = meta.get("medical_visit")
    if not isinstance(visit, dict):
        visit = {}
        meta["medical_visit"] = visit
    return meta


def _session_lookup(db: Session, active_org: ActiveOrganization, session_id: str) -> DBRecordingSession:
    row = (
        db.query(DBRecordingSession)
        .filter(
            DBRecordingSession.organization_id == active_org.organization.id,
            (DBRecordingSession.session_id == session_id) | (DBRecordingSession.id == int(session_id))
            if session_id.isdigit()
            else (DBRecordingSession.session_id == session_id),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="medical visit not found")
    meta = dict(row.processing_metadata or {})
    visit = meta.get("medical_visit")
    if row.meeting_type != _TRANSIENT_KIND or not isinstance(visit, dict):
        raise HTTPException(status_code=404, detail="medical visit not found")
    return row


def _status_alias(status_value: str | None) -> str:
    raw = (status_value or "").strip().lower()
    if raw in {"completed", "complete"}:
        return "completed"
    if raw in {"processing", "transcribing", "summarizing"}:
        return "processing"
    if raw in {"purged"}:
        return "purged"
    if raw in {"failed", "error"}:
        return "error"
    if raw in {"recording", "active"}:
        return "recording"
    return raw or "unknown"


def _best_summary(session: DBRecordingSession) -> Any:
    if isinstance(session.final_summary, dict) and session.final_summary:
        return session.final_summary
    return session.summary


def _best_transcript(session: DBRecordingSession) -> str:
    return (session.transcript_simple or session.transcript or "").strip()


def _diarized_payload(session: DBRecordingSession) -> dict[str, Any] | None:
    from services.speaker_service import sanitize_diarized_for_response

    diarized = session.transcript_diarized
    if not isinstance(diarized, dict):
        return None
    return sanitize_diarized_for_response(diarized)


def _shape_session(session: DBRecordingSession) -> dict[str, Any]:
    meta = dict(session.processing_metadata or {})
    visit = meta.get("medical_visit") or {}
    return {
        "id": session.session_id or str(session.id),
        "status": _status_alias(session.status),
        "mode": session.mode,
        "correlation_id": visit.get("correlation_id"),
        "source_label": visit.get("source_label"),
        "transcript_simple": _best_transcript(session),
        "transcript_diarized": _diarized_payload(session),
        "summary": session.summary,
        "final_summary": _best_summary(session),
        "ai_insights": session.ai_insights,
        "purged_at": visit.get("purged_at"),
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "ended_at": session.ended_at.isoformat() if session.ended_at else None,
    }


def _purge_local_audio(session: DBRecordingSession, org_slug: str) -> None:
    canonical_id = session.session_id or str(session.id)
    if session.audio_object_key and session.audio_storage_backend:
        media_storage.delete_object(
            backend=session.audio_storage_backend,
            key=session.audio_object_key,
        )
    if session.audio_file:
        try:
            Path(session.audio_file).unlink(missing_ok=True)
        except Exception:
            logger.warning("medical_visits: local audio delete failed path=%s", session.audio_file, exc_info=True)
    try:
        from api.recording import _audio_chunks_dir

        shutil.rmtree(_audio_chunks_dir(org_slug, canonical_id), ignore_errors=True)
    except Exception:
        logger.debug("medical_visits: chunk purge skipped", exc_info=True)


def _scrub_session(session: DBRecordingSession, *, purged_at: str) -> None:
    meta = _medical_visit_metadata(session)
    visit = meta["medical_visit"]
    visit["purged_at"] = purged_at
    visit["purged"] = True
    session.processing_metadata = meta
    session.status = "purged"
    session.audio_file = None
    session.audio_storage_backend = None
    session.audio_object_key = None
    session.transcript = None
    session.transcript_simple = None
    session.transcript_diarized = None
    session.summary = None
    session.final_summary = None
    session.summary_preview = None
    session.ai_insights = None
    session.generated_emails = None
    flag_modified(session, "processing_metadata")


async def require_medical_visit_caller(
    request: Request,
    db: Session = Depends(get_db),
    x_meetingops_org: Optional[str] = Header(default=None, alias=ORG_HEADER_NAME),
) -> MedicalVisitCaller:
    del x_meetingops_org  # header is consumed by resolve_active_organization via request
    claims = _verify_medical_visit_token(_bearer_token(request))
    groups = _token_groups(claims)
    user = AuthService.get_or_create_sso_user(
        db,
        email=_token_email(claims),
        username=(claims.get("preferred_username") if isinstance(claims.get("preferred_username"), str) else None),
        full_name=(claims.get("name") if isinstance(claims.get("name"), str) else None),
        is_superuser=is_global_admin(groups),
        groups=groups,
    )
    active_org = resolve_active_organization(db, request, user)
    return MedicalVisitCaller(user=user, active_org=active_org, claims=claims)


@router.post("")
async def create_medical_visit(
    payload: MedicalVisitCreateRequest,
    caller: MedicalVisitCaller = Depends(require_medical_visit_caller),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    existing = None
    if payload.client_session_key:
        existing = (
            db.query(DBRecordingSession)
            .filter(DBRecordingSession.organization_id == caller.active_org.organization.id)
            .all()
        )
        existing = next(
            (
                row
                for row in existing
                if isinstance((row.processing_metadata or {}).get("medical_visit"), dict)
                and (row.processing_metadata or {}).get("medical_visit", {}).get("client_session_key")
                == payload.client_session_key
                and row.meeting_type == _TRANSIENT_KIND
            ),
            None,
        )
    if existing is not None:
        return _shape_session(existing)

    now = datetime.now(timezone.utc)
    correlation_id = (
        (payload.correlation_id or "").strip()
        or f"gm-medvisit-{uuid.uuid4()}"
    )[:255]
    session_id = str(uuid.uuid4())
    source_label = (payload.source_label or "Glitter Mane medical visit").strip()[:255] or "Glitter Mane medical visit"
    metadata = {
        "medical_visit": {
            "correlation_id": correlation_id,
            "client_session_key": (payload.client_session_key or "").strip() or None,
            "source_label": source_label,
            "created_by": "glitter-mane",
            "transient": True,
            "purge_after_pull": True,
        }
    }
    row = DBRecordingSession(
        session_id=session_id,
        name=source_label,
        title=source_label,
        description="Transient Glitter Mane medical visit",
        status="recording",
        mode="always_on",
        created_at=now,
        started_at=now,
        duration=0.0,
        user_id=caller.user.id,
        organization_id=caller.active_org.organization.id,
        meeting_type=_TRANSIENT_KIND,
        tags=[_TRANSIENT_TAG],
        processing_metadata=metadata,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "medical_visits: created session=%s org=%s user=%s correlation=%s",
        row.session_id,
        caller.active_org.organization.slug,
        caller.user.email,
        correlation_id,
    )
    return _shape_session(row)


@router.get("/{session_id}")
async def get_medical_visit(
    session_id: str,
    caller: MedicalVisitCaller = Depends(require_medical_visit_caller),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    row = _session_lookup(db, caller.active_org, session_id)
    return _shape_session(row)


@router.post("/{session_id}/audio")
async def upload_medical_visit_audio(
    session_id: str,
    file: UploadFile = File(...),
    caller: MedicalVisitCaller = Depends(require_medical_visit_caller),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Attach a recorded visit's audio and run Meeting-Ops' completion pipeline
    (Parakeet STT + pyannote diarization) against this transient session. The
    diarized transcript lands on the session; Glitter Mane polls, scribes the
    clinical note, then purges. Reuses the same UploadJob/upload_pipeline_queue
    machinery as reprocess (action="attach" reuses the target session)."""
    # Lazy imports so the medical-visit router has no import-time coupling to the
    # (heavier) uploads module.
    from api.uploads import _job_dir, upload_pipeline_queue
    from database.models import UploadJob

    row = _session_lookup(db, caller.active_org, session_id)
    meta = _medical_visit_metadata(row)
    if meta["medical_visit"].get("purged_at"):
        raise HTTPException(status_code=409, detail="medical visit already purged")

    org_slug = caller.active_org.organization.slug
    job_id = uuid.uuid4()
    job_dir = _job_dir(org_slug, str(job_id))
    job_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".wav", ".mp3", ".m4a", ".mp4", ".webm", ".ogg", ".flac", ".aac"}:
        ext = ".wav"
    dest = job_dir / f"visit{ext}"
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            size += len(chunk)
    if size == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="empty audio upload")

    row.audio_file = str(dest)
    row.status = "processing"
    meta["medical_visit"]["audio_received_at"] = datetime.now(timezone.utc).isoformat()
    row.processing_metadata = meta
    flag_modified(row, "processing_metadata")

    job = UploadJob(
        organization_id=caller.active_org.organization.id,
        user_id=caller.user.id,
        upload_id=job_id,
        filename=dest.name,
        content_type=file.content_type or "application/octet-stream",
        action="attach",
        total_size=size,
        bytes_received=size,
        chunks_received=1,
        total_chunks=1,
        stage="queued",
        progress_pct=0,
        session_id=row.id,
    )
    db.add(job)
    db.commit()
    db.refresh(row)
    await upload_pipeline_queue.enqueue(str(job_id))
    logger.info(
        "medical_visits: audio attached session=%s job=%s bytes=%s org=%s",
        row.session_id, job_id, size, org_slug,
    )
    return _shape_session(row)


@router.delete("/{session_id}")
async def purge_medical_visit(
    session_id: str,
    caller: MedicalVisitCaller = Depends(require_medical_visit_caller),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    row = _session_lookup(db, caller.active_org, session_id)
    meta = dict(row.processing_metadata or {})
    visit = meta.get("medical_visit") or {}
    if visit.get("purged_at"):
        return {"status": "purged", "id": row.session_id or str(row.id)}
    _purge_local_audio(row, caller.active_org.organization.slug)
    _scrub_session(row, purged_at=datetime.now(timezone.utc).isoformat())
    db.commit()
    logger.info(
        "medical_visits: purged session=%s org=%s correlation=%s",
        row.session_id,
        caller.active_org.organization.slug,
        visit.get("correlation_id"),
    )
    return {"status": "purged", "id": row.session_id or str(row.id)}
