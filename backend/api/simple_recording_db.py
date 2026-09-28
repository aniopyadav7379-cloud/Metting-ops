"""
Database-backed Simple Recording API
Fixed version that persists to PostgreSQL
"""

from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks, Query
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Literal
from sqlalchemy.orm import Session, defer
from sqlalchemy import and_, desc, or_, func
import base64
import asyncio
import os
import re
import json
import logging
import uuid
import time
from contextlib import suppress

# Database imports
from database.database import get_db
from database.models import RecordingSession as DBRecordingSession, Transcription, ChatHistory

# Import the working audio service
from services.working_audio_service import audio_service
from services.transcription_service import transcription_service
from services.real_whisper_service import real_whisper_service
from services.unified_agent_service import unified_agent_service


# Vocabulary replacement helper
def apply_vocabulary_replacements(text: str, db_session, organization_id: int) -> str:
    """Apply vocabulary term replacements to transcript text."""
    if not text:
        return text
    try:
        from models.vocabulary import CustomVocabulary
        terms = db_session.query(CustomVocabulary).filter(
            CustomVocabulary.organization_id == organization_id,
            CustomVocabulary.is_active == True
        ).order_by(CustomVocabulary.priority.desc()).all()

        if not terms:
            return text

        import re
        replaced_count = 0
        for term in terms:
            if term.regex_pattern:
                try:
                    flags = 0 if term.case_sensitive else re.IGNORECASE
                    new_text = re.sub(term.regex_pattern, term.expansion, text, flags=flags)
                    if new_text != text:
                        replaced_count += 1
                        text = new_text
                except re.error as e:
                    logger.warning(f"Invalid regex pattern for term '{term.term}': {e}")
            else:
                if term.case_sensitive:
                    if term.term in text:
                        text = text.replace(term.term, term.expansion)
                        replaced_count += 1
                else:
                    pattern = re.compile(re.escape(term.term), re.IGNORECASE)
                    new_text = pattern.sub(term.expansion, text)
                    if new_text != text:
                        replaced_count += 1
                        text = new_text

        if replaced_count > 0:
            logger.info(f"Applied {replaced_count} vocabulary replacements")
        return text
    except Exception as e:
        logger.warning(f"Vocabulary replacement failed (non-fatal): {e}")
        return text


from auth.dependencies import get_current_organization, get_current_user
from auth.organization import ActiveOrganization
from auth.tier import gate_feature_for_caller
from auth.models import Organization, User, UserOrganization

import redis.asyncio as aioredis

router = APIRouter(prefix="/api/simple", tags=["simple-recording-db"])
logger = logging.getLogger(__name__)
DIARIZATION_TIMEOUT_SECONDS = float(
    os.getenv("DIARIZATION_TIMEOUT_SECONDS", "1800")
)

# Redis URL (matches docker-compose port)
_REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6381")

# Track auto-stop monitor tasks so they can be cancelled on manual stop
_auto_stop_tasks: Dict[str, asyncio.Task] = {}

class CreateSessionRequest(BaseModel):
    name: str
    description: Optional[str] = ""
    project_app: Optional[str] = None      # 'project-ops' | 'crisis-ops' | None
    project_id: Optional[int] = None
    project_slug: Optional[str] = None
    # Optional idempotency key. A retried create (double-click / network
    # retry) with the SAME key inside a 60s window returns the existing
    # session instead of inserting a duplicate row. None => unchanged.
    client_session_key: Optional[str] = None


class UpdateSessionProjectRequest(BaseModel):
    project_app: Optional[str] = None      # 'project-ops' | 'crisis-ops' | None ('' clears)
    project_id: Optional[int] = None
    project_slug: Optional[str] = None

class ParticipantSummary(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    role: Optional[str] = None


class SpeakerSummary(BaseModel):
    # Named speaker on a session, for the list cards: the confirmed
    # SpeakerSessionLink -> SpeakerProfile, with the MO-local photo_url
    # (no live Contact-Ops call) so cards can show speaker avatars cheaply.
    name: str
    photo_url: Optional[str] = None
    raw_label: Optional[str] = None


class SessionResponse(BaseModel):
    id: str
    name: str
    title: Optional[str] = None
    description: str
    created_at: str
    status: str
    duration: float
    audio_file: Optional[str] = None
    participants: List[ParticipantSummary] = []
    tags: List[str] = []
    summary_preview: Optional[str] = None
    speaker_count: int = 0
    speakers: List[SpeakerSummary] = []
    project_app: Optional[str] = None
    project_id: Optional[int] = None
    project_slug: Optional[str] = None
    organization_id: Optional[int] = None
    organization_name: Optional[str] = None
    # User-editable "when the meeting actually happened". ISO date /
    # 24h time. Backfilled from started_at / created_at by alembic 027.
    meeting_date: Optional[str] = None
    meeting_time: Optional[str] = None
    # Provenance (v3.35.x): whose account captured/uploaded this session.
    # Workspaces are shared — multiple members' recordings live side by
    # side (two people can even record the SAME call), so cards/details
    # show whose copy this is.
    recorded_by: Optional[str] = None


def _recorded_by_name(db: Session, user_id: Optional[int]) -> Optional[str]:
    """Display name of the account that captured/uploaded a session.

    Single-row lookup — used on the DETAIL payload only; the list path
    batches via recorder_names_by_user_id to avoid an N+1."""
    if user_id is None:
        return None
    from auth.models import User as _User

    u = db.query(_User).filter(_User.id == user_id).first()
    if u is None:
        return None
    return u.full_name or u.username or u.email or f"User {u.id}"


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to naive-UTC so window math never mixes
    tz-aware and naive values (Postgres returns naive, but defaults are
    stamped tz-aware before the first round-trip)."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _safe_iso_or_none(value: Any) -> Optional[str]:
    """Serialize persisted timestamps without letting malformed legacy data
    break a session-detail response."""
    if not isinstance(value, datetime):
        return None
    try:
        return value.isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _session_recording_window(
    session: DBRecordingSession,
) -> Optional[tuple[datetime, datetime, float]]:
    """(start, end, duration_seconds) of a session's recording window, or
    None when it can't be computed / is too short to matter.

    start = started_at when set, else created_at; end = start + duration.
    Sessions with no usable start, null duration, or duration <= 60s are
    excluded — sub-minute blips produce garbage overlap signals."""
    start = _naive_utc(getattr(session, "started_at", None)) or _naive_utc(
        session.created_at
    )
    if start is None:
        return None
    try:
        duration = float(session.duration) if session.duration is not None else None
    except (TypeError, ValueError):
        return None
    if duration is None or duration <= 60.0:
        return None
    return start, start + timedelta(seconds=duration), duration


def _related_sessions(db: Session, session: DBRecordingSession) -> List[dict]:
    """Same-meeting duplicate detection, v1: detect + link (NO merge).

    Two members of the same workspace can each record the SAME call; this
    finds OTHER completed sessions in the SAME organization recorded by a
    DIFFERENT user whose recording window overlaps this one's by more than
    50% of the SHORTER session. Computed on read (no migration, no
    stamping — self-healing as sessions change). Cross-org copies must
    NEVER link: the tenant wall is the whole point.

    Never raises — a detail page must not 500 because of this affordance.
    """
    try:
        window = _session_recording_window(session)
        if window is None or session.organization_id is None:
            return []
        start, end, duration = window

        # Coarse SQL bound (cheap): same org, completed, plausible duration,
        # start within +/- 24h of this session's start. Precise overlap math
        # happens in Python below.
        coarse_lo = start - timedelta(hours=24)
        coarse_hi = start + timedelta(hours=24)
        candidate_start = func.coalesce(
            DBRecordingSession.started_at, DBRecordingSession.created_at
        )
        candidates = (
            db.query(DBRecordingSession)
            .filter(
                DBRecordingSession.id != session.id,
                DBRecordingSession.organization_id == session.organization_id,
                DBRecordingSession.status == "completed",
                DBRecordingSession.duration.isnot(None),
                DBRecordingSession.duration > 60.0,
                candidate_start >= coarse_lo,
                candidate_start <= coarse_hi,
            )
            .order_by(candidate_start.asc())
            .limit(200)
            .all()
        )

        related: List[dict] = []
        for cand in candidates:
            try:
                # DIFFERENT user required. None == None also (correctly)
                # fails to link — two ownerless rows aren't evidence of two
                # people recording one call.
                if cand.user_id == session.user_id:
                    continue
                cand_window = _session_recording_window(cand)
                if cand_window is None:
                    continue
                c_start, c_end, c_duration = cand_window
                overlap = (min(end, c_end) - max(start, c_start)).total_seconds()
                shorter = min(duration, c_duration)
                if overlap <= 0.5 * shorter:
                    continue
                related.append(
                    {
                        "id": cand.id,
                        "name": cand.name or cand.title or f"Session {cand.id}",
                        "recorded_by": _recorded_by_name(db, cand.user_id),
                        "started_at": c_start.isoformat(),
                        "duration": c_duration,
                    }
                )
            except Exception as exc:  # noqa: BLE001 — skip one bad candidate
                logger.warning(
                    "related-sessions: skipping candidate %s for session %s: %s",
                    getattr(cand, "id", "?"),
                    session.id,
                    exc,
                )
                continue
        return related
    except Exception as exc:  # noqa: BLE001 — never fail the detail page
        logger.warning(
            "related-sessions detection failed for session %s: %s",
            getattr(session, "id", "?"),
            exc,
        )
        return []


def _summary_preview(session: DBRecordingSession, max_chars: int = 220) -> Optional[str]:
    """Pull a short plain-text snippet of the AI summary for the list view.

    Prefers ai_insights.summary (most recent + LLM-cleaned), then
    final_summary.summary, then the legacy session.summary JSON blob.
    """
    candidates = []
    if isinstance(session.ai_insights, dict):
        candidates.append(session.ai_insights.get("summary"))
    if isinstance(session.final_summary, dict):
        candidates.append(session.final_summary.get("summary"))
        candidates.append(session.final_summary.get("text"))
    if session.summary:
        try:
            raw = json.loads(session.summary) if isinstance(session.summary, str) else session.summary
            if isinstance(raw, dict):
                candidates.append(raw.get("summary"))
                candidates.append(raw.get("text"))
            elif isinstance(raw, str):
                candidates.append(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            candidates.append(str(session.summary))
    for c in candidates:
        if c and isinstance(c, str):
            # Strip markdown decoration so a list-view snippet stays readable.
            text = c
            for marker in ("**", "##", "###", "####", "`"):
                text = text.replace(marker, "")
            # Bullet asterisks and leading dashes: turn into a separator so we
            # still get list-readable prose ("X. Y. Z" feel) without raw
            # markdown clutter.
            text = re.sub(r"(^|\n)\s*[\*\-•]\s+", r"\1", text)
            text = " ".join(text.split())
            if not text:
                continue
            if len(text) <= max_chars:
                return text
            return text[:max_chars - 1].rstrip() + "…"
    return None


def _speaker_count(session: DBRecordingSession) -> int:
    """Count distinct speakers from transcript_diarized.speakers if present."""
    td = session.transcript_diarized
    if isinstance(td, dict):
        speakers = td.get("speakers")
        if isinstance(speakers, list):
            return len([s for s in speakers if s])
    return 0


def _scoped_session_query(db: Session, organization_id: int):
    return db.query(DBRecordingSession).filter(
        DBRecordingSession.organization_id == organization_id
    )


def _participants_payload(session: DBRecordingSession) -> List[dict]:
    """Coerce the JSONB column into a clean response list. Tolerates legacy
    NULLs and stray non-list values without 500ing the caller."""
    raw = session.participants
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        out.append({
            "id": str(p.get("id") or ""),
            "name": str(p.get("name") or ""),
            "email": p.get("email") or None,
            "role": p.get("role") or None,
        })
    return out


def _tags_payload(session: DBRecordingSession) -> List[str]:
    """Coerce the TEXT[] column into a clean list of strings."""
    raw = session.tags
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str) and t]


def _speakers_by_session_batch(
    db: Session, sessions: List[DBRecordingSession]
) -> Dict[int, List[dict]]:
    """Named speakers for a PAGE of sessions in ONE query — confirmed
    SpeakerSessionLink rows joined to their org SpeakerProfile (display_name +
    MO-local photo_url). Returns ``{session_id: [{name, photo_url, raw_label}]}``,
    distinct-by-name and capped per session.

    This replaces a per-session ``_speakers_payload`` lookup that was an N+1 on
    the list endpoint (one speaker query × up to ``limit`` sessions = visibly
    slow cards). Best-effort: never raises into the list builder."""
    out: Dict[int, List[dict]] = {}
    ids = [s.id for s in sessions if s.id is not None]
    if not ids:
        return out
    try:
        from database.models import SpeakerSessionLink, SpeakerProfile
        rows = (
            db.query(
                SpeakerSessionLink.session_id,
                SpeakerProfile.display_name,
                SpeakerProfile.photo_url,
                SpeakerSessionLink.raw_label,
            )
            .join(SpeakerProfile, SpeakerSessionLink.speaker_id == SpeakerProfile.id)
            .filter(SpeakerSessionLink.session_id.in_(ids))
            .filter(SpeakerSessionLink.speaker_id.isnot(None))
            .order_by(
                SpeakerSessionLink.session_id.asc(),
                SpeakerSessionLink.raw_label.asc(),
            )
            .all()
        )
        seen: Dict[int, set] = {}
        for sid, name, photo, raw in rows:
            if not name:
                continue
            names = seen.setdefault(sid, set())
            if name in names:
                continue
            names.add(name)
            bucket = out.setdefault(sid, [])
            if len(bucket) < 12:
                bucket.append({"name": name, "photo_url": photo, "raw_label": raw})
        return out
    except Exception:
        return {}


def _action_items_payload(db: Session, session: DBRecordingSession) -> List[dict]:
    """Persisted action items for this session, ordered the way the LLM
    produced them (sort_order asc, with created_at as a stable tiebreaker
    for manual additions made after summarize).

    Falls back to an empty list on lookup error so the session detail
    response keeps working even if the action_items table is unavailable
    (very early after migration). Frontend retains a JSON-column fallback
    parser for that window."""
    try:
        from database.models import ActionItem
        rows = (
            db.query(ActionItem)
            .filter(ActionItem.session_id == session.id)
            .order_by(ActionItem.sort_order.asc(), ActionItem.created_at.asc(), ActionItem.id.asc())
            .all()
        )
    except Exception as exc:
        logger.warning(f"action_items lookup failed for session {session.id}: {exc}")
        return []

    out: List[dict] = []
    for item in rows:
        out.append({
            "id": item.id,
            "text": item.text,
            "owner": item.owner,
            "due_date": _safe_iso_or_none(item.due_date),
            "status": item.status,
            "sort_order": item.sort_order,
            "source": item.source,
            "created_at": _safe_iso_or_none(item.created_at),
            "completed_at": _safe_iso_or_none(item.completed_at),
            "project_ops_link_state": item.project_ops_link_state or "local_only",
            "project_ops_proposal_id": item.project_ops_proposal_id,
            "project_ops_task_id": item.project_ops_task_id,
            "project_ops_task_url": item.project_ops_task_url,
            "project_ops_project_number": item.project_ops_project_number,
            "project_ops_task_status": item.project_ops_task_status,
            "project_ops_submitted_at": _safe_iso_or_none(
                item.project_ops_submitted_at
            ),
            "project_ops_last_sync_attempt_at": _safe_iso_or_none(
                item.project_ops_last_sync_attempt_at
            ),
            "project_ops_last_synced_at": _safe_iso_or_none(
                item.project_ops_last_synced_at
            ),
            "project_ops_remote_updated_at": _safe_iso_or_none(
                item.project_ops_remote_updated_at
            ),
            "project_ops_sync_error": item.project_ops_sync_error,
            "project_ops_retry_count": int(item.project_ops_retry_count or 0),
        })
    return out


def _find_active_session_by_client_key(
    db: Session,
    organization_id: int,
    client_session_key: Optional[str],
) -> Optional[DBRecordingSession]:
    """An existing session in this org carrying ``client_session_key`` in
    processing_metadata, still active, created in the last 60s — or None.

    De-duplicates retried create() calls. Returns None immediately for a
    missing key, so the no-key path is unchanged. Active = active /
    recording / processing (an /api/simple create starts as 'active'). The
    coarse query is org + active-status only; the JSON key match + the 60s
    window are checked in Python (portable across Postgres JSONB and the
    SQLite test fixture, which lacks the ``->>`` operator). Never raises."""
    if not client_session_key:
        return None
    try:
        now = datetime.now(timezone.utc)
        candidates = (
            _scoped_session_query(db, organization_id)
            .filter(DBRecordingSession.status.in_(("active", "recording", "processing")))
            .order_by(desc(DBRecordingSession.created_at))
            .limit(50)
            .all()
        )
        for cand in candidates:
            meta = cand.processing_metadata or {}
            if not (isinstance(meta, dict) and meta.get("client_session_key") == client_session_key):
                continue
            created = _naive_utc(cand.created_at)
            if created is None:
                continue
            if (now.replace(tzinfo=None) - created) > timedelta(seconds=60):
                continue
            return cand
    except Exception as exc:  # noqa: BLE001 — dedup is best-effort
        logger.warning(
            "create dedup probe failed (org=%s key=%s): %s",
            organization_id, client_session_key, exc,
        )
    return None


def _get_session_for_org(
    db: Session,
    organization_id: int,
    session_id: str,
    user: Optional[User] = None,
    min_level: str = "view",
) -> Optional[DBRecordingSession]:
    """Resolve a session in the caller's ACTIVE org; when that misses and a
    `user` is given, fall back to cross-org resolution gated by
    has_session_access (the same pattern as speakers._get_session_or_404 —
    a personal-org session viewed while active in another org, or a shared
    session, is still usable by someone who can see it).

    `min_level` is the minimum access level the fallback accepts:
    "view" for read endpoints, "edit" for mutations. The strict same-org
    path is unaffected — org members keep exactly the access they had.
    """
    session = _scoped_session_query(db, organization_id).filter(
        DBRecordingSession.session_id == session_id
    ).first()
    if session:
        return session

    try:
        int_id = int(session_id)
    except (TypeError, ValueError):
        int_id = None

    if int_id is not None:
        session = _scoped_session_query(db, organization_id).filter(
            DBRecordingSession.id == int_id
        ).first()
        if session:
            return session

    if user is None:
        return None

    from api.session_permissions import _get_session_by_str_id, has_session_access

    candidate = _get_session_by_str_id(db, str(session_id))
    if not candidate:
        return None
    level = has_session_access(candidate.id, user, db)
    _RANK = {"denied": 0, "view": 1, "comment": 2, "edit": 3}
    if _RANK.get(level, 0) >= _RANK.get(min_level, 1):
        return candidate
    return None
    
@router.post("/recording-sessions")
async def create_session(
    request: CreateSessionRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
) -> SessionResponse:
    """Create a new recording session in database"""

    # Idempotency: a retried create with the SAME client_session_key (double-
    # click / network retry) inside a 60s window returns the existing session
    # instead of inserting a duplicate. Org-scoped; None key => unchanged.
    existing = _find_active_session_by_client_key(
        db, active_org.organization.id, request.client_session_key
    )
    if existing is not None:
        logger.info(
            "create-session idempotent reuse: org=%s key=%s -> existing session %s",
            active_org.organization.id, request.client_session_key, existing.id,
        )
        return SessionResponse(
            id=existing.session_id or str(existing.id),
            name=existing.name,
            description=existing.description or "",
            created_at=existing.created_at.isoformat() if existing.created_at else "",
            status=existing.status,
            duration=float(existing.duration or 0),
        )

    session_id = str(uuid.uuid4())

    # Create database session
    # Validate project_app if provided
    project_app = request.project_app or None
    if project_app and project_app not in ("project-ops", "crisis-ops"):
        raise HTTPException(status_code=400, detail="project_app must be 'project-ops' or 'crisis-ops'")

    db_session = DBRecordingSession(
        session_id=session_id,
        name=request.name,
        title=request.name,  # Use name as title
        description=request.description or "",
        status="active",
        created_at=datetime.now(timezone.utc),
        duration=0.0,
        user_id=current_user.id,
        organization_id=active_org.organization.id,
        project_app=project_app,
        project_id=request.project_id if project_app else None,
        project_slug=request.project_slug if project_app else None,
        processing_metadata=(
            {"client_session_key": request.client_session_key}
            if request.client_session_key
            else None
        ),
    )

    db.add(db_session)
    db.commit()
    db.refresh(db_session)

    logger.info(
        f"Created DB session: {session_id} "
        f"(project_app={db_session.project_app}, project_id={db_session.project_id})"
    )

    return SessionResponse(
        id=session_id,
        name=db_session.name,
        description=db_session.description or "",
        created_at=db_session.created_at.isoformat(),
        status=db_session.status,
        duration=0.0
    )

_VALID_SORT_KEYS = {"created_at_desc", "meeting_date_desc", "meeting_date_asc"}


def _encode_sessions_cursor(session: DBRecordingSession) -> str:
    payload = json.dumps({
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "id": session.id,
    }, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_sessions_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(payload["created_at"]), int(payload["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid sessions cursor") from exc


@router.get("/recording-sessions")
async def list_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    limit: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None),
    tags: Optional[str] = Query(None, description="Comma-separated tags; AND filter"),
    sort: Optional[str] = Query(
        None,
        description=(
            "Sort order. One of created_at_desc (default), "
            "meeting_date_desc, meeting_date_asc. When meeting_date is "
            "null the sort falls back to started_at."
        ),
    ),
    bulk_import_job_id: Optional[str] = Query(
        None,
        description="Filter sessions created by this bulk import job (UUID)",
    ),
    include_all_my_orgs: bool = Query(
        False,
        description="Return sessions from every organization the caller belongs to.",
    ),
    all_tenants: bool = Query(
        False,
        description=(
            "PLATFORM-ADMIN god-view: return sessions across EVERY organization "
            "in the system, not just the caller's. Honored ONLY for superusers "
            "(silently ignored otherwise). Explicit, opt-in cross-tenant access "
            "for support/ops — the default views are member-scoped for privacy."
        ),
    ),
) -> dict:
    """List all recording sessions from database"""

    org_names_by_id: dict[int, str] = {}
    is_super = bool(getattr(current_user, "is_superuser", False))
    if all_tenants and is_super:
        # Explicit, opt-in PLATFORM-ADMIN god-view: every org's sessions. This is
        # a deliberate cross-tenant action (the UI surfaces it only to superusers
        # behind a clearly-labelled toggle), NOT a silent default — so we log it
        # for an audit trail of who looked across tenants and when.
        logger.warning(
            "PLATFORM-ADMIN all-tenants session view used by user_id=%s (%s)",
            getattr(current_user, "id", "?"),
            getattr(current_user, "email", "?"),
        )
        query = db.query(DBRecordingSession)
    elif include_all_my_orgs:
        # Scope to the orgs the caller is a MEMBER of — for EVERYONE, including
        # superusers. A superuser is an operations role (admin panels, user
        # management, system config), NOT a licence to browse customers' private
        # meeting content in the normal session list. Cross-org content access
        # must come from real org membership, a shared workspace, or the explicit
        # opt-in all-tenants god-view above — never the platform-admin flag by
        # default. (Previously a superuser's "all orgs" view returned every
        # session in the system, so the platform owner saw every tenant's private
        # meetings on the default view — removed.)
        org_ids = [
            row.organization_id
            for row in db.query(UserOrganization.organization_id)
            .filter(UserOrganization.user_id == current_user.id)
            .all()
        ]
        if not org_ids:
            query = db.query(DBRecordingSession).filter(False)
        else:
            query = db.query(DBRecordingSession).filter(
                DBRecordingSession.organization_id.in_(org_ids)
            )
    else:
        query = _scoped_session_query(db, active_org.organization.id)
    query = query.options(
        defer(DBRecordingSession.transcript),
        defer(DBRecordingSession.transcript_simple),
        defer(DBRecordingSession.transcript_diarized),
        defer(DBRecordingSession.summary),
        defer(DBRecordingSession.final_summary),
        defer(DBRecordingSession.progressive_summaries),
        defer(DBRecordingSession.ai_insights),
        defer(DBRecordingSession.generated_emails),
        defer(DBRecordingSession.extra_data),
    )
    if tags:
        wanted = [t.strip() for t in tags.split(",") if t.strip()]
        if wanted:
            query = query.filter(DBRecordingSession.tags.contains(wanted))
    # Hide empty always-on sessions: when the user clicks Start always-on
    # but never produces audio (mic blocked, browser bug, accidental click),
    # the start endpoint still creates a session row. Without filtering, the
    # dashboard fills up with "Always-on YYYY-MM-DD HH:MM" rows that have
    # no transcript and no audio. Filter them out here.
    query = query.filter(
        ~(
            (DBRecordingSession.mode == "always_on")
            & ((DBRecordingSession.transcript.is_(None)) | (DBRecordingSession.transcript == ""))
            & ((DBRecordingSession.audio_file.is_(None)) | (DBRecordingSession.audio_file == ""))
        )
    )

    if bulk_import_job_id:
        from database.models import BulkImportFile
        import uuid as _uuid
        try:
            job_uuid = _uuid.UUID(bulk_import_job_id)
            file_rows = db.query(BulkImportFile).filter(BulkImportFile.job_id == job_uuid, BulkImportFile.session_id_when_created.isnot(None)).all()
            session_ids = [str(f.session_id_when_created) for f in file_rows if f.session_id_when_created]
            if session_ids:
                query = query.filter(DBRecordingSession.session_id.in_(session_ids))
            else:
                query = query.filter(False)
        except ValueError:
            # Malformed bulk_import_job_id - silently ignore the filter
            # rather than 400'ing. The list view still returns the org's
            # sessions; the caller passed garbage and gets the unfiltered
            # view back, which is the safer fail-open shape here.
            pass
    sort_key = sort if sort in _VALID_SORT_KEYS else "created_at_desc"
    if cursor and sort_key != "created_at_desc":
        raise HTTPException(status_code=400, detail="Cursor pagination requires created_at_desc sort")
    if cursor:
        cursor_created_at, cursor_id = _decode_sessions_cursor(cursor)
        query = query.filter(or_(
            DBRecordingSession.created_at < cursor_created_at,
            and_(
                DBRecordingSession.created_at == cursor_created_at,
                DBRecordingSession.id < cursor_id,
            ),
        ))
    if sort_key in ("meeting_date_desc", "meeting_date_asc"):
        # Coalesce so a null meeting_date falls back to started_at and
        # finally to created_at. Keeps newly-uploaded sessions visible
        # in date-sorted views even before the user edits a date in.
        effective = func.coalesce(
            DBRecordingSession.meeting_date,
            func.date(DBRecordingSession.started_at),
            func.date(DBRecordingSession.created_at),
        )
        order_clause = desc(effective) if sort_key == "meeting_date_desc" else effective
        query = query.order_by(order_clause, desc(DBRecordingSession.id))
    else:
        query = query.order_by(desc(DBRecordingSession.created_at), desc(DBRecordingSession.id))

    rows = query.limit(limit + 1).all()
    has_more = len(rows) > limit
    sessions = rows[:limit]
    session_org_ids = sorted({
        int(session.organization_id)
        for session in sessions
        if session.organization_id is not None
    })
    if session_org_ids:
        org_names_by_id = {
            int(org.id): org.name
            for org in db.query(Organization)
            .filter(Organization.id.in_(session_org_ids))
            .all()
        }

    # One query for the whole page's named speakers (avoids an N+1 — see
    # _speakers_by_session_batch). Keyed by session.id.
    speakers_by_session = _speakers_by_session_batch(db, sessions)

    # Provenance: one batch query for the recorders' display names (same
    # no-N+1 pattern as org_names_by_id above).
    recorder_names_by_user_id: dict[int, str] = {}
    session_user_ids = sorted({
        int(s.user_id) for s in sessions if s.user_id is not None
    })
    if session_user_ids:
        from auth.models import User as _User
        recorder_names_by_user_id = {
            int(u.id): (u.full_name or u.username or u.email or f"User {u.id}")
            for u in db.query(_User).filter(_User.id.in_(session_user_ids)).all()
        }

    result = []
    for session in sessions:
        result.append(SessionResponse(
            id=session.session_id or str(session.id),
            name=session.name or session.title or f"Session {session.id}",
            title=session.title,
            description=session.description or "",
            created_at=session.created_at.isoformat() if session.created_at else datetime.now(timezone.utc).isoformat(),
            status=session.status or "completed",
            duration=session.duration or 0.0,
            audio_file=session.audio_file,
            participants=_participants_payload(session),
            tags=_tags_payload(session),
            summary_preview=session.summary_preview,
            speaker_count=int(session.speaker_count or 0),
            speakers=speakers_by_session.get(session.id, []),
            project_app=session.project_app,
            project_id=session.project_id,
            project_slug=session.project_slug,
            organization_id=session.organization_id,
            organization_name=org_names_by_id.get(session.organization_id),
            meeting_date=session.meeting_date.isoformat() if session.meeting_date else None,
            meeting_time=session.meeting_time.isoformat() if session.meeting_time else None,
            recorded_by=(
                recorder_names_by_user_id.get(int(session.user_id))
                if session.user_id is not None
                else None
            ),
        ))

    return {
        "items": result,
        "next_cursor": _encode_sessions_cursor(sessions[-1]) if has_more and sessions else None,
        "has_more": has_more,
    }


class SearchResult(BaseModel):
    id: str
    name: str
    description: str
    created_at: str
    status: str
    duration: float
    snippet: Optional[str] = None
    match_field: Optional[str] = None

@router.get("/recording-sessions/search")
async def search_sessions(
    q: str = Query(..., min_length=1, description="Search query string"),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
) -> List[SearchResult]:
    """Search recording sessions across name, transcript, and transcript_simple columns.
    Returns matching sessions with a snippet of the matching text."""
    
    search_term = f"%{q}%"
    
    sessions = _scoped_session_query(db, active_org.organization.id).filter(
        or_(
            DBRecordingSession.name.ilike(search_term),
            DBRecordingSession.title.ilike(search_term),
            DBRecordingSession.description.ilike(search_term),
            DBRecordingSession.transcript.ilike(search_term),
            DBRecordingSession.transcript_simple.ilike(search_term),
        )
    ).order_by(desc(DBRecordingSession.created_at)).limit(limit).all()
    
    results = []
    for session in sessions:
        snippet = None
        match_field = None
        q_lower = q.lower()
        
        # Determine which field matched and extract a snippet
        if session.name and q_lower in session.name.lower():
            snippet = session.name
            match_field = "name"
        elif session.title and q_lower in session.title.lower():
            snippet = session.title
            match_field = "title"
        elif session.description and q_lower in session.description.lower():
            snippet = _extract_snippet(session.description, q)
            match_field = "description"
        elif session.transcript_simple and q_lower in session.transcript_simple.lower():
            snippet = _extract_snippet(session.transcript_simple, q)
            match_field = "transcript"
        elif session.transcript and q_lower in str(session.transcript).lower():
            # transcript may be JSON, extract text for snippet
            try:
                trans_data = json.loads(session.transcript) if isinstance(session.transcript, str) else session.transcript
                if isinstance(trans_data, dict) and "text" in trans_data:
                    text = trans_data["text"]
                elif isinstance(trans_data, dict) and "segments" in trans_data:
                    text = " ".join(seg.get("text", "") for seg in trans_data["segments"])
                else:
                    text = str(session.transcript)
                snippet = _extract_snippet(text, q)
            except Exception:
                snippet = _extract_snippet(str(session.transcript), q)
            match_field = "transcript"
        
        results.append(SearchResult(
            id=session.session_id or str(session.id),
            name=session.name or session.title or f"Session {session.id}",
            description=session.description or "",
            created_at=session.created_at.isoformat() if session.created_at else "",
            status=session.status or "completed",
            duration=session.duration or 0.0,
            snippet=snippet,
            match_field=match_field,
        ))
    
    return results


# === Semantic / Vector Search (Qdrant) ===

@router.get("/recording-sessions/semantic-search")
async def semantic_search_sessions(
    q: str = Query(..., min_length=1, description="Natural language search query"),
    limit: int = Query(10, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Semantic search across meetings using vector similarity.
    Understands meaning, not just keywords — e.g. 'meetings about budget planning'
    will match transcripts discussing finances even without the exact words."""
    # v3.23.0 tier gate: cross-meeting semantic search is a TEXT-corpus
    # operation. Basic + higher get it; Free is browser-only.
    gate_feature_for_caller(current_user, "cross_meeting_search", active_org)
    try:
        from services.semantic_search_service import semantic_search
        results = semantic_search.search(
            query=q,
            limit=limit,
            organization_id=active_org.organization.id,
        )
        return {"query": q, "results": results, "count": len(results)}
    except Exception as e:
        logger.error(f"Semantic search error: {e}")
        raise HTTPException(status_code=500, detail=f"Semantic search failed: {str(e)}")


@router.post("/semantic-search/reindex")
async def reindex_all_sessions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Reindex all completed sessions into the vector store.
    Run this once to backfill, then new sessions are indexed automatically."""
    try:
        from services.semantic_search_service import semantic_search
        result = semantic_search.reindex_all(
            db,
            organization_id=active_org.organization.id,
        )
        return {"status": "ok", **result}
    except Exception as e:
        logger.error(f"Reindex error: {e}")
        raise HTTPException(status_code=500, detail=f"Reindex failed: {str(e)}")


@router.get("/semantic-search/stats")
async def get_semantic_search_stats(
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Get vector search index statistics."""
    try:
        from services.semantic_search_service import semantic_search
        return semantic_search.get_stats(organization_id=active_org.organization.id)
    except Exception as e:
        return {"initialized": False, "error": str(e)}


def _extract_snippet(text: str, query: str, context_chars: int = 80) -> str:
    """Extract a snippet of text around the first occurrence of the query string."""
    if not text:
        return ""
    idx = text.lower().find(query.lower())
    if idx == -1:
        return text[:160] + ("..." if len(text) > 160 else "")
    
    start = max(0, idx - context_chars)
    end = min(len(text), idx + len(query) + context_chars)
    
    snippet = text[start:end]
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    
    return snippet

@router.get("/recording-sessions/{session_id}")
async def get_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
) -> dict:
    """Get a specific recording session from database with error handling"""
    try:
        from services.speaker_service import (
            hydrate_diarized_for_response,
            sanitize_diarized_for_response,
        )
        # Find by session_id (UUID string), org-scoped first.
        session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user)

        # Fallback: if the session isn't in the active org, the caller
        # might still be a per-meeting collaborator (or hold access via
        # a future project-membership lookup). Resolve cross-org and
        # delegate the final permission check to has_session_access so
        # external collaborators can fetch meeting detail without being
        # in the org's users table.
        if not session:
            from api.session_permissions import (
                _get_session_by_str_id,
                has_session_access,
            )
            candidate = _get_session_by_str_id(db, session_id)
            if candidate and has_session_access(candidate.id, current_user, db) != "denied":
                session = candidate

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        # related_sessions exposes metadata (titles, recorder names, times)
        # of OTHER sessions in the HOST org — only actual MEMBERS of that
        # org may see it. A caller who reached this session through the
        # collaborator fallback above (or any other non-member path) gets
        # an empty list (v3.36.0 hardening).
        from auth.models import UserOrganization
        caller_is_org_member = bool(
            session.organization_id is not None
            and db.query(UserOrganization)
            .filter(
                UserOrganization.user_id == current_user.id,
                UserOrganization.organization_id == session.organization_id,
            )
            .first()
        )

        # Parse transcription if it exists - with safe error handling
        transcription_data = None
        if session.transcript:
            try:
                if isinstance(session.transcript, str):
                    transcription_data = json.loads(session.transcript)
                else:
                    transcription_data = session.transcript
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Failed to parse transcript JSON for session {session_id}: {e}")
                transcription_data = {"text": str(session.transcript)} if session.transcript else None
        
        # Get transcription segments with error handling
        segments = []
        try:
            transcriptions = db.query(Transcription).filter(
                Transcription.session_id == session.id
            ).order_by(Transcription.start_time).all()
            
            for trans in transcriptions:
                try:
                    segments.append({
                        "text": trans.text or "",
                        "speaker": trans.speaker or "Unknown",
                        "start": trans.start_time or 0,
                        "end": trans.end_time or 0,
                        "confidence": trans.confidence or 0
                    })
                except Exception as e:
                    logger.warning(f"Error processing transcription segment: {e}")
                    continue
                    
        except Exception as e:
            logger.warning(f"Error loading transcription segments for session {session_id}: {e}")
            segments = []
        
        # Build transcription data
        if segments and not transcription_data:
            transcription_data = {"segments": segments}
        elif transcription_data and segments:
            transcription_data["segments"] = segments
        elif not transcription_data:
            transcription_data = {"text": "", "segments": []}

        # Legacy rows can carry the same biometric embedding arrays in
        # ``transcript``/``transcription`` that newer rows keep in
        # ``transcript_diarized``.  Sanitize every public alias so clients
        # cannot recover vectors by reading an older compatibility field.
        transcription_data = sanitize_diarized_for_response(transcription_data)
        safe_legacy_transcript = session.transcript or ""
        if isinstance(safe_legacy_transcript, str):
            try:
                parsed_legacy = json.loads(safe_legacy_transcript)
                if isinstance(parsed_legacy, dict):
                    safe_legacy_transcript = json.dumps(
                        sanitize_diarized_for_response(parsed_legacy)
                    )
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(safe_legacy_transcript, dict):
            safe_legacy_transcript = sanitize_diarized_for_response(
                safe_legacy_transcript
            )
        
        # Parse summary with error handling
        summary_data = None
        if session.summary:
            try:
                if session.summary != "null":
                    if isinstance(session.summary, str):
                        summary_data = json.loads(session.summary)
                    else:
                        summary_data = session.summary
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(f"Failed to parse summary JSON for session {session_id}: {e}")
                summary_data = {"text": str(session.summary)} if session.summary else None
        
        # Return session data with all new fields
        return {
            "id": session.session_id or str(session.id),
            "session_id": session.session_id or str(session.id),
            "name": session.name or session.title or f"Session {session.id}",
            "title": session.title or session.name or f"Session {session.id}",
            "description": session.description or "",
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "ended_at": session.ended_at.isoformat() if session.ended_at else None,
            # User-editable meeting date/time. ISO date / 24h time. Null
            # only if backfill (alembic 027) found no started_at / created_at
            # to pull from, or the user explicitly cleared the value.
            "meeting_date": session.meeting_date.isoformat() if session.meeting_date else None,
            "meeting_time": session.meeting_time.isoformat() if session.meeting_time else None,
            "status": session.status or "unknown",
            "duration": float(session.duration) if session.duration else 0.0,
            "audio_file": session.audio_file or "",
            # Old fields for backward compatibility
            "transcription": transcription_data,
            "transcription_segments": segments,
            "summary": summary_data,
            "transcript": safe_legacy_transcript,
            # New enhanced fields
            "transcript_simple": session.transcript_simple or "",
            "transcript_diarized": (
                hydrate_diarized_for_response(session)
                if session.transcript_diarized else transcription_data
            ),
            "progressive_summaries": session.progressive_summaries or [],
            "final_summary": session.final_summary or summary_data,
            "ai_insights": session.ai_insights or summary_data,
            "metadata": session.processing_metadata or {},
            "generated_emails": session.generated_emails or [],
            # Legacy fields
            "user_id": session.user_id,
            # Provenance: whose account captured/uploaded this session
            # (workspaces are shared; two members can record the same call).
            "recorded_by": _recorded_by_name(db, session.user_id),
            # Same-meeting duplicate detection (v1 detect + link, no merge):
            # other completed same-org sessions by a DIFFERENT user whose
            # recording window overlaps this one's by >50% of the shorter
            # session. Computed on read; internally guarded (never 500s),
            # empty list when none. ORG MEMBERS ONLY — external
            # collaborators must not see the host org's other sessions.
            "related_sessions": (
                _related_sessions(db, session) if caller_is_org_member else []
            ),
            "meeting_type": session.meeting_type,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
            # Project linking (Phase 2)
            "project_app": session.project_app,
            "project_id": session.project_id,
            "project_slug": session.project_slug,
            "organization_id": session.organization_id,
            "organization_name": (
                db.query(Organization.name)
                .filter(Organization.id == session.organization_id)
                .scalar()
            ),
            # Conference Room linkage. `room_id` is the canonical "this is a
            # room session" signal — non-null means the live-summary UI
            # should pull slices from the server-rolled store instead of
            # rolling them client-side.
            "room_id": str(session.room_id) if session.room_id else None,
            "room_source_id": (
                str(session.room_source_id) if session.room_source_id else None
            ),
            "room_name": session.room_name,
            # Per-session participants (attendees). Editable from the
            # session detail sidebar; feeds the email-attendees default.
            "participants": _participants_payload(session),
            # Free-form per-session tags. Editable from the header chips.
            "tags": _tags_payload(session),
            # First-class action items derived from the summarizer JSON and
            # promoted by services.action_items_extractor. Status-aware so
            # the UI can flip done/doing/cancelled and have it stick.
            "action_items": _action_items_payload(db, session),
            # Brigade integration Phase 1: surfaces the per-session
            # Brigade graph deep-link to the frontend so SessionDetails
            # can render the "View in Brigade graph" affordance only
            # for sessions that have been synced. brigade_graph_node_id
            # is the canonical entity name we passed to Brigade's
            # store_entity; brigade_synced_at stamps the last
            # successful write; brigade_graph_url is pre-built so the
            # client doesn't need to know the tenancy mode.
            "brigade_synced": bool(getattr(session, "brigade_graph_node_id", None)),
            "brigade_synced_at": (
                session.brigade_synced_at.isoformat()
                if getattr(session, "brigade_synced_at", None)
                else None
            ),
            "knowledge_graph": {
                "status": getattr(session, "brigade_sync_status", None) or (
                    "synced" if getattr(session, "brigade_graph_node_id", None) else "pending"
                ),
                "synced_at": session.brigade_synced_at.isoformat() if getattr(session, "brigade_synced_at", None) else None,
                "attempted_at": session.brigade_sync_attempted_at.isoformat() if getattr(session, "brigade_sync_attempted_at", None) else None,
                "error": getattr(session, "brigade_sync_error", None),
                "attempt_count": getattr(session, "brigade_sync_attempt_count", 0) or 0,
                "retryable": (getattr(session, "brigade_sync_status", None) or "pending") in {"pending", "failed"},
            },
        }
        
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Error retrieving session {session_id}: {e}")
        logger.error(f"Exception type: {type(e)}")
        logger.error(f"Exception details: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Internal server error retrieving session: {str(e)}"
        )

class StartRecordingRequest(BaseModel):
    device_id: Optional[str] = None


async def _monitor_auto_stop(session_id: str, db_session_id: int):
    """Background task that listens for an auto-stop Redis event from the
    LiveRecordingTranscriptionService and finalizes the recording when
    prolonged silence is detected.

    Subscribes to ``recording:{session_id}:auto-stop`` and, on receipt,
    stops the audio capture, updates the DB session, and schedules
    background transcription processing.
    """
    redis_client = None
    pubsub = None
    try:
        redis_client = await aioredis.from_url(_REDIS_URL, decode_responses=True)
        pubsub = redis_client.pubsub()
        channel = f"recording:{session_id}:auto-stop"
        await pubsub.subscribe(channel)
        logger.info(f"Auto-stop monitor subscribed to {channel}")

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            logger.warning(f"Auto-stop event received for session {session_id}")

            try:
                event_data = json.loads(message["data"])
                silence_secs = event_data.get("silence_duration_seconds", "unknown")
                logger.info(f"  Reason: prolonged silence ({silence_secs}s)")
            except (json.JSONDecodeError, TypeError):
                pass

            # 1. Stop ffmpeg recording (graceful SIGINT -> WAV header)
            from services.working_audio_service import audio_service as _audio_svc
            success, audio_file = _audio_svc.stop_recording(session_id)
            if success and audio_file:
                audio_file = os.path.normpath(os.path.abspath(audio_file))
            if not success:
                logger.error(f"Auto-stop: failed to stop audio capture: {audio_file}")

            # 2. Stop live transcription monitoring (already set is_active=False,
            #    but cancel the task explicitly for clean shutdown)
            try:
                from services.live_recording_transcription import live_recording_transcription
                await live_recording_transcription.stop_monitoring()
                logger.info(f"Auto-stop: stopped live transcription for {session_id}")
            except Exception as e:
                logger.error(f"Auto-stop: error stopping live transcription: {e}")

            # 3. Update the DB session
            from database.database import SessionLocal
            db = SessionLocal()
            try:
                session = db.query(DBRecordingSession).filter(
                    DBRecordingSession.session_id == session_id
                ).first()
                if session:
                    session.status = "processing"
                    session.ended_at = datetime.now(timezone.utc)
                    if session.started_at:
                        started = session.started_at
                        ended = session.ended_at
                        if started.tzinfo is None:
                            started = started.replace(tzinfo=timezone.utc)
                        if ended.tzinfo is None:
                            ended = ended.replace(tzinfo=timezone.utc)
                        session.duration = (ended - started).total_seconds()
                    if not session.audio_file and audio_file:
                        session.audio_file = audio_file
                    db.commit()
                    logger.info(f"Auto-stop: session {session_id} marked as processing")

                    # 4. Schedule background transcription + AI processing
                    asyncio.create_task(
                        process_recording(
                            session_id=session.session_id,
                            audio_file=audio_file if success else (session.audio_file or ""),
                            db_session_id=session.id,
                        )
                    )
                    logger.info(f"Auto-stop: scheduled background processing for {session_id}")
                else:
                    logger.error(f"Auto-stop: session {session_id} not found in DB")
            except Exception as e:
                logger.error(f"Auto-stop: DB error finalizing session: {e}")
                db.rollback()
            finally:
                db.close()

            # Clean up our task reference and exit the listener
            _auto_stop_tasks.pop(session_id, None)
            break

    except asyncio.CancelledError:
        logger.info(f"Auto-stop monitor cancelled for session {session_id}")
    except Exception as e:
        logger.error(f"Auto-stop monitor error for session {session_id}: {e}")
    finally:
        if pubsub:
            try:
                await pubsub.unsubscribe()
                await pubsub.close()
            except Exception:
                pass
        if redis_client:
            try:
                await redis_client.close()
            except Exception:
                pass


@router.post("/recording-sessions/{session_id}/start")
async def start_recording(
    session_id: str,
    body: Optional[StartRecordingRequest] = None,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Start recording for a session. Optionally specify device_id to select audio input."""

    # v3.18.1 tier gate: paid-tier server processing — starting a server
    # recording triggers STT/diarization/summary at stop, all server-side.
    # Free is browser-only and must never reach this path.
    gate_feature_for_caller(current_user, "canonical_reprocess", active_org)

    # Get session from database - handle both session_id and integer id
    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user, min_level="edit")

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Start recording with audio service (it will generate the file path)
    device_id = body.device_id if body else None
    success, file_path = audio_service.start_recording(session_id, device_id=device_id)

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to start recording: {file_path}")

    # Normalize the audio file path to prevent double-nesting
    file_path = os.path.normpath(os.path.abspath(file_path))

    # Update database with correct column names
    session.status = "recording"
    session.started_at = datetime.now(timezone.utc)
    session.audio_file = file_path  # Note: column is audio_file, not audio_file_path
    db.commit()
    
    logger.info(f"Started recording for session {session_id}")
    
    # Start live transcription monitoring (will wait for file internally)
    try:
        from services.live_recording_transcription import live_recording_transcription
        
        # Start monitoring task - it will handle waiting for the file
        asyncio.create_task(live_recording_transcription.start_monitoring(
            file_path,   # audio_file parameter
            session_id   # session_id parameter
        ))
        logger.info(f"Started live transcription monitoring for session {session_id}")
        logger.info(f"  Will monitor file: {file_path}")
        logger.info(f"  Transcription every 15 seconds, summaries every 500 words")
            
    except Exception as e:
        logger.error(f"Failed to start live transcription: {e}")
    
    # Start unified agent for progressive AI summaries with GPU acceleration
    try:
        asyncio.create_task(
            unified_agent_service.start_meeting_analysis(session_id)
        )
        logger.info(f"🚀 Started unified agent for progressive AI summaries on session {session_id}")
        logger.info(f"   Using GPU-accelerated llama.cpp on port 11437")
    except Exception as e:
        logger.error(f"Failed to start unified agent: {e}")
    
    # Start auto-stop monitor (listens for prolonged silence event from live transcription)
    try:
        task = asyncio.create_task(
            _monitor_auto_stop(session_id, session.id)
        )
        _auto_stop_tasks[session_id] = task
        logger.info(f"Started auto-stop monitor for session {session_id} "
                     f"(silence threshold: {20 * 15}s)")
    except Exception as e:
        logger.error(f"Failed to start auto-stop monitor: {e}")

    return {
        "status": "recording",
        "session_id": session_id,
        "message": "Recording started successfully"
    }

@router.post("/recording-sessions/{session_id}/stop")
async def stop_recording(
    session_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Stop recording and trigger transcription"""
    # v3.29.2 defense-in-depth: /stop schedules the server completion pass
    # (transcribe + diarize + identify + index). /start is already gated on
    # canonical_reprocess, so a real Free/Basic user can't have a server
    # recording to stop — this closes the direct-/stop-without-/start vector
    # and makes the server-compute gate explicit. gate_feature_for_caller
    # bypasses the internal room-recorder token, so Conference Room is unaffected.
    gate_feature_for_caller(current_user, "canonical_reprocess", active_org)

    # Get session from database - handle both session_id and integer id
    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user, min_level="edit")
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Cancel the auto-stop monitor if it's running (manual stop takes priority)
    auto_stop_task = _auto_stop_tasks.pop(session_id, None)
    if auto_stop_task and not auto_stop_task.done():
        auto_stop_task.cancel()
        logger.info(f"Cancelled auto-stop monitor for session {session_id} (manual stop)")

    try:
        # Stop recording
        success, audio_file = audio_service.stop_recording(session_id)

        if not success:
            # In-memory recorder state is gone — the backend was restarted /
            # crashed mid-recording, or this is the browser-first /
            # DISABLE_LOCAL_AUDIO path where the server never held an ffmpeg
            # process at all. The OLD behavior here was to 500 + stamp
            # status='error', which is exactly what lost a real 2h recording:
            # the chunks were durably on disk, but stop_recording() only
            # consults wiped in-memory flags. We now NEVER 500 for this case —
            # we look for whatever survived on the host volume (always-on
            # chunk dir, then a half-written WAV) and finalize from that, or
            # mark an honest no_audio terminal state. The recovery is wrapped
            # so a genuine DB/ffmpeg failure still 500s, but only AFTER the
            # attempt — a recoverable recording is never thrown away.
            return await _recover_and_finalize_stop(
                session, active_org, db, background_tasks, session_id
            )

        # ---- Happy path (live in-memory ffmpeg, e.g. Conference Room) ----
        # UNCHANGED from before: stop_recording() returned True with the WAV
        # path, so we finalize exactly as today. The recovery helper is NOT
        # reached on this branch — blast radius stays minimal.
        # Normalize the audio file path to prevent double-nesting
        if audio_file:
            audio_file = os.path.normpath(os.path.abspath(audio_file))

        # Update session in database
        session.status = "processing"
        session.ended_at = datetime.now(timezone.utc)
        if session.started_at:
            # Handle mixed naive/aware datetimes (from before timezone migration)
            started = session.started_at
            ended = session.ended_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=timezone.utc)
            session.duration = (ended - started).total_seconds()

        # Save the audio file path if not already set
        if not session.audio_file and audio_file:
            session.audio_file = audio_file

        db.commit()

        # Stop live transcription monitoring
        try:
            from services.live_recording_transcription import live_recording_transcription
            asyncio.create_task(live_recording_transcription.stop_monitoring())
            logger.info(f"Stopped live transcription for session {session_id}")
        except Exception as e:
            logger.error(f"Error stopping live transcription: {e}")

        # Schedule background transcription and processing
        background_tasks.add_task(
            process_recording,
            session_id=session.id if isinstance(session.id, int) else session.session_id,
            audio_file=audio_file,
            db_session_id=session.id
        )

        return {
            "status": "processing",
            "session_id": session_id,
            "duration": session.duration,
            "audio_file": audio_file,
            "message": "Recording stopped, processing transcription..."
        }

    except HTTPException:
        # A deliberate HTTP error (e.g. a 500 the recovery helper raised
        # AFTER attempting recovery) — propagate as-is, do NOT downgrade the
        # row to a generic 'error' on top of an already-considered outcome.
        raise
    except Exception as e:
        logger.error(f"Error stopping recording: {e}")
        session.status = "error"
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


async def _recover_and_finalize_stop(
    session: DBRecordingSession,
    active_org: ActiveOrganization,
    db: Session,
    background_tasks: BackgroundTasks,
    session_id: str,
) -> Dict[str, Any]:
    """Restart-resilient finalize for the legacy /stop endpoint.

    Reached ONLY when ``audio_service.stop_recording()`` returned False —
    i.e. the singleton holds no live in-memory state for this session (a
    backend restart/crash wiped ``is_recording`` / ``current_session_id``,
    or this is the browser-first path that never ran a server ffmpeg). The
    recoverable truth lives entirely on the host volume, so we finalize from
    whatever durably survived rather than 500'ing on stale memory:

      (a) ALWAYS-ON CHUNK DIR — if the browser streamed ~30s chunks to
          ``ALWAYS_ON_DIR/<org>/<session>/full_audio/`` (the exact dir
          /finalize-audio + the reprocess worker already reassemble from,
          with ZERO in-memory dependency), queue the canonical reprocess and
          return recovered_from='always_on_chunks'. This is the centerpiece
          fix for the lost-2h-recording incident.
      (b) HALF-WRITTEN WAV — else a partial ``recording_{sid}_*.wav`` left by
          a SIGKILL'd conference-room ffmpeg, handed to process_recording.
      (c) NOTHING — else an honest 200 no_audio (privacy / mic-blocked /
          true DISABLE_LOCAL_AUDIO no-capture). We do NOT stamp
          status='error': there is genuinely nothing to lose, and a cryptic
          500 is exactly the UX we're removing.

    We always finalize the recording-window bookkeeping (ended_at/duration)
    so the session is a clean terminal/processing row, never a dangling
    'recording'. Local imports of the recording helpers + enqueue_reprocess
    match the codebase's circular-import-avoidance pattern.
    """
    # Finalize the recording window regardless of which branch we take —
    # the meeting is over even if no audio reached the server.
    session.ended_at = datetime.now(timezone.utc)
    if session.started_at and (session.duration is None or session.duration == 0):
        started = session.started_at
        ended = session.ended_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        session.duration = (ended - started).total_seconds()

    # Stop live transcription monitoring (best-effort, same as happy path).
    try:
        from services.live_recording_transcription import live_recording_transcription
        asyncio.create_task(live_recording_transcription.stop_monitoring())
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error stopping live transcription during recovery: {e}")

    canonical_id = session.session_id or str(session.id)

    # ---- (a) Always-on chunks on disk -> canonical reprocess ----
    try:
        from api.recording import _audio_chunks_dir, _list_chunk_files

        chunks_dir = _audio_chunks_dir(active_org.organization.slug, canonical_id)
        chunk_files = _list_chunk_files(chunks_dir)
    except Exception as e:  # noqa: BLE001 — never let a path probe 500 the stop
        logger.warning(
            "recovery: chunk-dir probe failed for session %s: %s", session_id, e
        )
        chunk_files = []

    if chunk_files:
        logger.info(
            "stop recovery: session %s has %d always-on chunks on disk — "
            "finalizing from disk after lost in-memory state",
            session_id, len(chunk_files),
        )
        metadata = dict(session.processing_metadata or {})
        audio_state = dict(metadata.get("full_audio") or {})
        audio_state.update({
            "status": "queued",
            "source": "legacy_stop_recovery",
            "recovered_at": datetime.now(timezone.utc).isoformat(),
            "recovered_chunk_count": len(chunk_files),
        })
        metadata["full_audio"] = audio_state
        metadata["reprocess_status"] = "queued"
        session.processing_metadata = metadata
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(session, "processing_metadata")
        except Exception:  # noqa: BLE001
            pass
        session.status = "processing"
        db.commit()

        from workers.reprocess_workers import enqueue_reprocess
        await enqueue_reprocess(session.id, background_tasks=background_tasks)

        return {
            "status": "processing",
            "session_id": session_id,
            "recovered": True,
            "recovered_from": "always_on_chunks",
            "message": "Recording recovered after an interruption and is now processing.",
        }

    # ---- (b) Half-written WAV on disk -> process_recording ----
    wav_path = audio_service._find_orphan_wav_on_disk(session_id)
    if wav_path:
        wav_path = os.path.normpath(os.path.abspath(wav_path))
        logger.info(
            "stop recovery: session %s finalizing from orphan WAV %s",
            session_id, wav_path,
        )
        session.audio_file = wav_path
        session.status = "processing"
        db.commit()
        background_tasks.add_task(
            process_recording,
            session_id=session.id if isinstance(session.id, int) else session.session_id,
            audio_file=wav_path,
            db_session_id=session.id,
        )
        return {
            "status": "processing",
            "session_id": session_id,
            "recovered": True,
            "recovered_from": "disk_wav",
            "message": "Recording recovered after an interruption and is now processing.",
        }

    # ---- (c) Nothing survived -> honest, non-error terminal state ----
    logger.info(
        "stop recovery: session %s had no durable audio on the server "
        "(privacy / browser-first / no-capture) — marking no_audio",
        session_id,
    )
    metadata = dict(session.processing_metadata or {})
    metadata["stop_outcome"] = "no_audio"
    session.processing_metadata = metadata
    try:
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(session, "processing_metadata")
    except Exception:  # noqa: BLE001
        pass
    # 'cancelled' is the honest non-error terminal state for "the meeting
    # ended but the server captured nothing" — NOT 'error' (which the UI
    # renders as a failure + which stranded the lost recording). The empty
    # always-on list view already hides audio-less always-on rows, so this
    # doesn't clutter the dashboard.
    session.status = "cancelled"
    db.commit()
    return {
        "status": "no_audio",
        "session_id": session_id,
        "recovered": False,
        "message": "No audio was captured for this session on the server.",
    }

async def process_recording(session_id: str, audio_file: str, db_session_id: int):
    """Background task to process recording - with guaranteed completion"""
    from database.database import SessionLocal
    
    processing_start_time = time.time()  # Track processing start time
    
    db = SessionLocal()
    try:
        # Get fresh session from database
        session = db.query(DBRecordingSession).filter(
            DBRecordingSession.id == db_session_id
        ).first()
        
        if not session:
            logger.error(f"Session {db_session_id} not found in database")
            return
        
        logger.info(f"Starting guaranteed processing for session {session_id}")

        # Step 1: Transcribe + diarize via the canonical provider stack so
        # per-segment embeddings get preserved end-to-end. The legacy
        # real_whisper_service.transcribe_file path returns segments
        # without embeddings, so identify_speakers below would skip every
        # SPEAKER_xx label with reason=no_embedding and never auto-link
        # enrolled voices. The canonical stack (Parakeet STT + speaker-svc
        # diarize with return_embeddings=true + word-level alignment in
        # _assign_speakers_from_diarization) keeps the 256-d ECAPA embedding
        # on each utterance so identify_speakers can match Aaron's voice
        # automatically.
        transcription_result = None
        diarize_task = None
        try:
            logger.info(f"Starting canonical transcribe+diarize for {audio_file}")

            from api.uploads import _assign_speakers_from_diarization
            from services.providers.registry import get_provider_registry

            registry = get_provider_registry(db)

            # Launch speaker diarization first.  It reads the same audio
            # independently on the speaker GPU, so it can overlap the STT
            # request running on the Parakeet GPU.  We merge only after STT
            # has been persisted below.
            try:
                diar_provider = registry.get_diarization(session.organization_id)
                diarize_task = asyncio.create_task(
                    asyncio.wait_for(
                        diar_provider.diarize(audio_file),
                        timeout=DIARIZATION_TIMEOUT_SECONDS,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Could not launch concurrent diarization; will retry after "
                    "transcription: %s",
                    exc,
                )

            # 1a. Transcribe via STT provider (default: Parakeet 1.1B on midboy2)
            stt_provider = registry.get_stt(session.organization_id)
            stt_result = await asyncio.wait_for(
                stt_provider.transcribe(audio_file, language="en"),
                timeout=600  # 10 minute timeout for long always-on sessions
            )

            if not stt_result or not stt_result.get("segments"):
                logger.warning("Canonical STT returned no segments; falling back to legacy whisper")
                transcription_result = await asyncio.wait_for(
                    asyncio.to_thread(
                        real_whisper_service.transcribe_file,
                        audio_file,
                        diarize=True
                    ),
                    timeout=300
                )
            else:
                # Build the unified transcription_result shape downstream
                # code (vocabulary, summarizer, action items, metadata)
                # expects: {"text", "segments", "language", ...}.
                transcription_result = {
                    "text": stt_result.get("text", ""),
                    "segments": stt_result.get("segments", []),
                    "language": stt_result.get("language", "en"),
                    "model": stt_result.get("model", "parakeet-tdt-1.1b"),
                    "duration": stt_result.get("duration", 0.0),
                    "audio_duration": stt_result.get("duration", 0.0),
                    "rtf": stt_result.get("rtf"),
                    "npu_accelerated": False,
                }

            if transcription_result and transcription_result.get("segments"):
                # Persist transcription FIRST so _assign_speakers_from_diarization
                # has segments to label and so the word_timestamps land in
                # processing_metadata where _build_segments_from_words can
                # find them for Path 1 (word-level alignment).
                session.transcript_diarized = transcription_result
                simple_text = " ".join([s.get("text", "") for s in transcription_result.get("segments", [])])
                session.transcript_simple = simple_text
                session.transcript = json.dumps(transcription_result)

                # Stash word timestamps from the STT call (Parakeet emits
                # these natively) so _build_segments_from_words can do
                # word-level diarization alignment downstream.
                if stt_result and stt_result.get("words"):
                    pm = dict(session.processing_metadata or {})
                    pm["word_timestamps"] = stt_result["words"]
                    pm["has_word_timestamps"] = True
                    session.processing_metadata = pm
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(session, "processing_metadata")

                # 1b. Diarize via speaker-svc with return_embeddings=true so
                # each turn carries a 256-d ECAPA embedding for identify_speakers.
                diar_segments = []
                try:
                    if diarize_task is not None:
                        diar_segments = await diarize_task
                    else:
                        diar_provider = registry.get_diarization(
                            session.organization_id
                        )
                        diar_segments = await asyncio.wait_for(
                            diar_provider.diarize(audio_file),
                            timeout=DIARIZATION_TIMEOUT_SECONDS,
                        )
                    logger.info(
                        f"Canonical diarize: {len(diar_segments)} turns, "
                        f"embeddings={'yes' if (diar_segments and diar_segments[0].get('embedding')) else 'no'}"
                    )
                except Exception as exc:
                    logger.warning(f"Diarization step failed (non-fatal): {exc}")
                    from api.uploads import _flag_stage_needs_retry
                    from sqlalchemy.orm.attributes import flag_modified
                    _flag_stage_needs_retry(session, "diarization", exc)
                    flag_modified(session, "processing_metadata")
                    diar_segments = []
                else:
                    from api.uploads import _clear_stage_needs_retry
                    from sqlalchemy.orm.attributes import flag_modified
                    _clear_stage_needs_retry(session, "diarization")
                    flag_modified(session, "processing_metadata")

                if diar_segments:
                    # _assign_speakers_from_diarization writes labels + embeddings
                    # onto session.transcript_diarized.segments AND refreshes the
                    # Transcription rows. After this, identify_speakers (called in
                    # Step 6a below) can match enrolled voices automatically.
                    try:
                        _assign_speakers_from_diarization(session, diar_segments)
                    except Exception as exc:
                        logger.warning(f"Speaker merge step failed (non-fatal): {exc}")

                    # Re-read segments after merge — _assign_speakers_from_diarization
                    # rewrites the segments list (Path 1 produces utterances) and
                    # rewrites Transcription rows itself.
                    transcription_result = session.transcript_diarized or transcription_result
                else:
                    # No diarization → save segments straight to Transcription rows
                    # so list/detail views render text without speaker labels.
                    segment_count = 0
                    for segment in transcription_result.get("segments", []):
                        try:
                            trans = Transcription(
                                session_id=session.id,
                                text=segment.get("text", ""),
                                speaker=segment.get("speaker"),
                                start_time=float(segment.get("start", 0) or 0),
                                end_time=float(segment.get("end", 0) or 0),
                                confidence=float(segment.get("confidence", 0.95) or 0.95),
                            )
                            db.add(trans)
                            segment_count += 1
                        except Exception as e:
                            logger.warning(f"Failed to save segment: {e}")
                            continue
                    logger.info(f"Saved {segment_count} transcription segments (no diarization)")

                segments_after = (session.transcript_diarized or {}).get("segments", []) if isinstance(session.transcript_diarized, dict) else []
                emb_count = sum(1 for s in segments_after if s.get("embedding"))
                logger.info(
                    f"Step 1 complete: {len(segments_after)} segments, "
                    f"{emb_count} carry embeddings for identify_speakers"
                )
            else:
                logger.warning("No transcription segments generated")
                session.transcript = json.dumps({"text": "Transcription failed", "segments": []})

        except asyncio.TimeoutError:
            logger.error(f"Transcription timed out for session {session_id}")
            session.transcript = json.dumps({"text": "Transcription timed out", "segments": []})
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            session.transcript = json.dumps({"text": f"Transcription error: {str(e)}", "segments": []})
        finally:
            # A timeout/persistence error can leave the independently-running
            # speaker request alive. Cancel and *await* it so no GPU job or
            # pending task escapes this meeting's lifecycle.
            if diarize_task is not None:
                if not diarize_task.done():
                    diarize_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await diarize_task
        
        # Step 1.5: Apply vocabulary replacements to transcript
        try:
            if session.transcript_simple:
                session.transcript_simple = apply_vocabulary_replacements(
                    session.transcript_simple,
                    db,
                    session.organization_id,
                )

            if session.transcript_diarized and isinstance(session.transcript_diarized, dict):
                segments = session.transcript_diarized.get("segments", [])
                for seg in segments:
                    if seg.get("text"):
                        seg["text"] = apply_vocabulary_replacements(
                            seg["text"],
                            db,
                            session.organization_id,
                        )
                session.transcript_diarized = session.transcript_diarized

            if transcription_result and "segments" in transcription_result:
                for seg in transcription_result["segments"]:
                    if seg.get("text"):
                        seg["text"] = apply_vocabulary_replacements(
                            seg["text"],
                            db,
                            session.organization_id,
                        )
                session.transcript = json.dumps(transcription_result)

            db.flush()
            logger.info(f"Vocabulary replacements applied for session {session_id}")
        except Exception as e:
            logger.warning(f"Vocabulary replacement step failed (non-fatal): {e}")

        # Step 2a: Identify enrolled speakers + normalize labels BEFORE the
        # summary. v3.34.0 (audit findings #2/#4): identification used to run
        # AFTER summarization (old Step 5, post-commit), so the record->stop
        # summary was generated from a FLAT unattributed transcript and had
        # to guess who said what, while unmatched voices kept raw SPEAKER_xx
        # codes in the transcript UI. Identify first, then normalize the
        # leftovers to "Speaker N" + resync the Transcription rows the UI
        # reads — the same order as finalize (workers/finalize_workers.py
        # Step 0) and reprocess. Best-effort: a speaker-svc outage must not
        # fail processing.
        try:
            from services.speaker_service import identify_speakers
            ident_summary = identify_speakers(session, db)
            logger.info(f"Speaker identification: {ident_summary}")
        except Exception as e:
            logger.warning(f"Speaker identification failed (non-fatal): {e}")
        try:
            from services.speaker_labels import normalize_session_speaker_labels
            normalize_session_speaker_labels(session, db)
        except Exception as e:
            logger.warning(f"Speaker label normalization failed (non-fatal): {e}")

        # Step 2b: Generate AI summary with timeout protection
        try:
            if transcription_result and "segments" in transcription_result:
                full_text = " ".join([s.get("text", "") for s in transcription_result["segments"]])

                if full_text.strip():
                    # v3.34.0 (audit finding #4): summarize via the shared
                    # attributed-prompt path used by finalize/reprocess/upload.
                    # _summarize_session builds "Name: utterance" lines from
                    # the (now identified + normalized) diarized segments so
                    # the model reads attribution instead of guessing it,
                    # writes summary/final_summary, auto-titles (respecting
                    # title_user_set), persists action items, and shares the
                    # summary-input idempotency hash. Replaces the legacy
                    # flat-text summarizer call and its stale model-name log.
                    logger.info(f"Starting AI summary for session {session_id} (attributed prompt)")
                    from api.uploads import _summarize_session
                    await _summarize_session(db, session, template="standard")
                else:
                    logger.warning("No transcript text available for AI analysis")
                    session.summary = json.dumps({
                        "executive": "Meeting recorded but no transcription available.",
                        "bullets": ["Audio file captured", "Transcription failed"],
                        "title": f"Meeting {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
                    })
            else:
                logger.warning("No transcription available for AI analysis")
                session.summary = json.dumps({
                    "executive": "Meeting recorded but transcription failed.",
                    "bullets": ["Audio file saved", "Transcription processing failed"],
                    "title": f"Meeting {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
                })
                
        except asyncio.TimeoutError:
            logger.error(f"AI analysis timed out for session {session_id}")
            session.summary = json.dumps({
                "executive": "Meeting recorded successfully. AI analysis timed out.",
                "bullets": ["Audio and transcript captured", "AI processing unavailable"],
                "title": f"Meeting {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
            })
        except Exception as e:
            logger.error(f"AI analysis error: {e}")
            session.summary = json.dumps({
                "executive": "Meeting recorded successfully. AI analysis failed.",
                "bullets": ["Audio and transcript captured", f"AI error: {str(e)[:100]}"],
                "title": f"Meeting {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
            })
        
        # Step 3: Update metadata with processing stats
        processing_end_time = time.time()
        processing_duration = processing_end_time - processing_start_time  # Calculate actual duration
        
        # Calculate word count and speaker count
        word_count = 0
        speaker_count = 0
        if session.transcript_simple:
            word_count = len(session.transcript_simple.split())
        if session.transcript_diarized and isinstance(session.transcript_diarized, dict):
            speakers = set()
            for segment in session.transcript_diarized.get("segments", []):
                if segment.get("speaker"):
                    speakers.add(segment["speaker"])
            speaker_count = len(speakers)
        
        # Update metadata. Pull the actual STT model name + npu flag from
        # transcription_result so always-on sessions reflect whether they
        # ran through Parakeet (canonical) or the legacy NPU whisper fallback.
        metadata = session.processing_metadata or {}
        transcription_model = (
            (transcription_result or {}).get("model")
            if isinstance(transcription_result, dict)
            else None
        ) or "parakeet-tdt-1.1b"
        npu_accelerated = bool(
            (transcription_result or {}).get("npu_accelerated", False)
            if isinstance(transcription_result, dict)
            else False
        )
        # ai_model: stamp the live summarizer route instead of the stale
        # hardcoded legacy model label — the env-configured direct model
        # (MEETING_OPS_LLM_MODEL, the Qwen route _summarize_session prefers)
        # or the org's ProviderRegistry default when no direct route is set.
        ai_model = (
            os.getenv("MEETING_OPS_LLM_MODEL", "").strip()
            or os.getenv("MEETING_OPS_SUMMARIZER_MODEL", "").strip()
            or "provider-registry-default"
        )
        metadata.update({
            "word_count": word_count,
            "speaker_count": speaker_count,
            "npu_accelerated": npu_accelerated,
            "processing_time_ms": int(processing_duration * 1000) if processing_duration > 0 else 0,
            "transcription_model": transcription_model,
            "ai_model": ai_model,
            "processing_completed_at": datetime.now(timezone.utc).isoformat()
        })
        session.processing_metadata = metadata
        # _summarize_session commits + refreshes the session above, so this
        # in-place JSON mutation must be re-flagged or the Step 4 commit
        # silently drops word_count / processing_completed_at.
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(session, "processing_metadata")
        
        # Step 4: ALWAYS mark as completed (never leave in processing)
        session.status = "completed"
        session.updated_at = datetime.now(timezone.utc)

        # Promote action items into the first-class table so the dashboard
        # can render an interactive list. Best-effort; never block finalize.
        try:
            from services.action_items_extractor import persist_action_items
            persist_action_items(db, session)
        except Exception as exc:
            logger.warning(f"action_items promotion failed for session {session_id}: {exc}")

        try:
            db.commit()
            logger.info(f"✅ Successfully completed processing for session {session_id}")
            logger.info(f"   Status: {session.status}")
            logger.info(f"   Title: {session.title}")
            logger.info(f"   Has transcript: {bool(session.transcript)}")
            logger.info(f"   Has summary: {bool(session.summary)}")

            # Step 5: Index into the vector store for semantic search. Runs
            # AFTER Step 2a's identify_speakers + label normalization (the
            # v3.29.2 identify-before-index invariant, pinned by
            # test_stop_recording_index_order), so the indexed transcript is
            # built FROM speaker-named/normalized transcript_diarized
            # segments and RAG is speaker-aware, mirroring reprocess
            # Stage-5.9; fall back to transcript_simple when no diarized
            # segments exist.
            try:
                from services.semantic_search_service import semantic_search
                _segs = (
                    session.transcript_diarized.get("segments")
                    if isinstance(session.transcript_diarized, dict)
                    else None
                )
                if _segs:
                    _index_transcript = "\n".join(
                        f"{(seg.get('speaker') or 'Speaker')}: {(seg.get('text') or '').strip()}"
                        for seg in _segs
                        if (seg.get("text") or "").strip()
                    )
                else:
                    _index_transcript = session.transcript_simple or ""
                semantic_search.index_session(
                    session_id=session.session_id or str(session.id),
                    title=session.title or session.name or "",
                    transcript=_index_transcript,
                    summary=session.summary or "",
                    created_at=session.created_at.isoformat() if session.created_at else "",
                    organization_id=session.organization_id,
                )
                logger.info(f"   Indexed in vector store for semantic search (speaker-aware)")
            except Exception as e:
                logger.warning(f"Vector indexing failed (non-fatal): {e}")

        except Exception as e:
            logger.error(f"Failed to commit session updates: {e}")
            db.rollback()
        
    except Exception as e:
        logger.error(f"Critical error in background processing: {e}")
        
        # Even on critical error, try to mark as completed
        try:
            if session:
                session.status = "completed"  # Not "error" - frontend expects "completed"
                session.summary = json.dumps({
                    "executive": "Meeting recorded but processing failed.",
                    "bullets": ["Audio file captured", f"Processing error: {str(e)[:100]}"],
                    "title": f"Meeting {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
                })
                session.updated_at = datetime.now(timezone.utc)
                db.commit()
                logger.info(f"Marked session {session_id} as completed despite errors")
        except Exception as commit_error:
            logger.error(f"Failed to mark session as completed: {commit_error}")
            
    finally:
        db.close()

class RenameSessionRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    # ISO YYYY-MM-DD; empty string or null clears. The string form lets
    # the frontend send the native <input type="date"> value as-is.
    meeting_date: Optional[str] = None
    # 24h HH:MM[:SS]; empty string or null clears.
    meeting_time: Optional[str] = None


def _parse_meeting_date(value: Optional[str]):
    """Parse a YYYY-MM-DD into a date, or None to clear.

    Raises HTTPException(400) on malformed input so the client gets a
    helpful error instead of a 500.
    """
    if value is None or value == "":
        return None
    try:
        from datetime import date as _date
        return _date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"meeting_date must be YYYY-MM-DD, got {value!r}",
        )


def _parse_meeting_time(value: Optional[str]):
    """Parse a HH:MM[:SS] into a time, or None to clear."""
    if value is None or value == "":
        return None
    try:
        from datetime import time as _time
        # Native <input type="time"> emits HH:MM; ISO accepts both.
        return _time.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail=f"meeting_time must be HH:MM or HH:MM:SS, got {value!r}",
        )


@router.patch("/recording-sessions/{session_id}")
async def rename_session(
    session_id: str,
    request: RenameSessionRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Update user-editable session metadata.

    Supports title, description, meeting_date, meeting_time. Each is
    independently optional. Setting title flips title_user_set=True,
    which prevents the auto-summary step from overwriting the rename
    on the next reprocess. Setting meeting_date / meeting_time to "" or
    null clears the column back to NULL.
    """
    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user, min_level="edit")
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if request.title is not None:
        title = request.title.strip()
        if title:
            session.title = title[:200]
            session.name = title[:200]
            session.title_user_set = True
        else:
            # Empty title resets so auto-titling can take over again.
            session.title_user_set = False

    if request.description is not None:
        session.description = request.description

    # meeting_date / meeting_time. The Pydantic field being present (not
    # absent) is what triggers an update; "" clears, value writes.
    fields_set = request.model_dump(exclude_unset=True)
    if "meeting_date" in fields_set:
        session.meeting_date = _parse_meeting_date(request.meeting_date)
    if "meeting_time" in fields_set:
        session.meeting_time = _parse_meeting_time(request.meeting_time)

    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)

    return {
        "status": "updated",
        "session_id": session.session_id,
        "title": session.title,
        "title_user_set": session.title_user_set,
        "meeting_date": session.meeting_date.isoformat() if session.meeting_date else None,
        "meeting_time": session.meeting_time.isoformat() if session.meeting_time else None,
    }


@router.patch("/recording-sessions/{session_id}/project-link")
async def update_session_project_link(
    session_id: str,
    request: UpdateSessionProjectRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Set or clear the project link on an existing recording session.

    Pass project_app=None (or empty string) to clear the link.
    """
    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user, min_level="edit")
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Normalize: empty string means clear
    project_app = request.project_app or None
    if project_app and project_app not in ("project-ops", "crisis-ops"):
        raise HTTPException(status_code=400, detail="project_app must be 'project-ops' or 'crisis-ops'")

    if project_app:
        session.project_app = project_app
        session.project_id = request.project_id
        session.project_slug = request.project_slug
    else:
        # Clear the link
        session.project_app = None
        session.project_id = None
        session.project_slug = None

    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)

    logger.info(
        f"Updated project link for session {session_id}: "
        f"project_app={session.project_app}, project_id={session.project_id}"
    )

    return {
        "id": session.session_id or str(session.id),
        "project_app": session.project_app,
        "project_id": session.project_id,
        "project_slug": session.project_slug,
    }


def _delete_session_record(
    db: Session,
    session: DBRecordingSession,
    session_id: str,
) -> Optional[tuple[int, str]]:
    """Hard-delete one already-org-scoped session: local audio file, durable
    Garage copies, transcriptions, then the row itself + commit.

    This is the single source of truth for per-session deletion so the
    single-delete endpoint and the bulk-delete endpoint stay byte-for-byte
    identical. The caller MUST have resolved `session` via an org-scoped
    lookup (e.g. `_get_session_for_org`) — this helper does no auth/org
    checks of its own.
    """
    brigade_ref = None
    if session.brigade_graph_node_id:
        brigade_ref = (session.organization_id, session.brigade_graph_node_id)

    # Canonical string id used by the vector store + per-meeting chat history
    # (both keyed by this string, NOT by the integer PK/FK). Capture it BEFORE
    # db.delete(session) so it survives the row removal.
    canonical_id = session.session_id or str(session.id)

    # Delete audio file if it exists
    if session.audio_file and os.path.exists(session.audio_file):
        try:
            os.remove(session.audio_file)
        except OSError:
            pass

    # Purge any durable copies in Garage too (the whole session prefix:
    # audio + tts + import). Makes "delete my data" actually delete it.
    try:
        from services.session_media import purge_session_media
        removed = purge_session_media(session)
        if removed:
            logger.info(f"Purged {removed} Garage object(s) for session {session_id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Garage purge failed for session {session_id}: {exc}")

    # Delete transcriptions
    db.query(Transcription).filter(
        Transcription.session_id == session.id
    ).delete()

    # Delete this meeting's AI chat history. chat_history.session_key is a
    # plain string key with NO FK to recording_sessions, so nothing cascades
    # it — without this, transcript-derived PII survives "deletion". Org-scoped
    # as defense-in-depth. (The org-level cross-meeting RAG history is keyed
    # separately and intentionally left alone.)
    try:
        db.query(ChatHistory).filter(
            ChatHistory.session_key == canonical_id,
            ChatHistory.organization_id == session.organization_id,
        ).delete(synchronize_session=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Chat-history purge failed for session {session_id}: {exc}")

    # Delete session
    db.delete(session)
    db.commit()

    # Purge the vector-store embeddings so a deleted meeting no longer surfaces
    # in semantic search / cross-meeting RAG. Best-effort + import-on-use (keeps
    # qdrant off the cold-start path); mirrors the always-on discard path in
    # recording.py. Runs AFTER commit so a qdrant hiccup can't block the delete.
    # MUST run before `return brigade_ref` (the merge of the Brigade-cascade
    # return + this purge originally orphaned this block — keep the order).
    try:
        from services.semantic_search_service import semantic_search
        semantic_search.delete_session(canonical_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Qdrant cleanup failed for session {session_id}: {exc}")

    return brigade_ref


@router.delete("/recording-sessions/{session_id}")
async def delete_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Delete a recording session"""

    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user, min_level="edit")

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    brigade_ref = _delete_session_record(db, session, session_id)
    if brigade_ref:
        try:
            from services.brigade_writer import delete_session_from_brigade
            result = await delete_session_from_brigade(*brigade_ref)
            if not result.ok:
                logger.warning("Brigade delete failed session=%s: %s", session_id, result.detail)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Brigade delete failed session=%s: %s", session_id, exc)

    return {"message": "Session deleted successfully"}


class BulkDeleteRequest(BaseModel):
    session_ids: List[str]


@router.post("/recording-sessions/bulk-delete")
async def bulk_delete_sessions(
    request: BulkDeleteRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Hard-delete many sessions at once (multi-select on the Sessions page).

    Same auth + org-scoping as the single-session delete: every id is
    resolved through `_get_session_for_org`, so only sessions in the
    caller's ACTIVE organization are deletable. Ids that don't resolve in
    this org (missing, already deleted, or belonging to another org) are
    skipped and reported in `failed` rather than aborting the batch — we
    never leak cross-org existence and one bad id never blocks the rest.

    Reuses the EXACT same per-session deletion logic as the single delete
    (`_delete_session_record`) in a loop, committing each row independently
    so a mid-batch failure can't roll back already-deleted sessions.

    Body: { "session_ids": ["<uuid-or-int>", ...] }
    Returns: { "deleted": <int>, "failed": [{ "id": "...", "reason": "..." }] }
    """
    deleted = 0
    failed: List[Dict[str, str]] = []

    # De-dup while preserving order so a repeated id isn't double-counted
    # (the second pass would 404 once the row is gone).
    seen: set[str] = set()
    for raw_id in request.session_ids:
        session_id = (raw_id or "").strip()
        if not session_id or session_id in seen:
            continue
        seen.add(session_id)

        session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user, min_level="edit")
        if not session:
            failed.append({"id": session_id, "reason": "not_found"})
            continue

        try:
            brigade_ref = _delete_session_record(db, session, session_id)
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            logger.warning(f"Bulk delete failed for session {session_id}: {exc}")
            failed.append({"id": session_id, "reason": str(exc)[:200]})
            continue
        if brigade_ref:
            try:
                from services.brigade_writer import delete_session_from_brigade
                result = await delete_session_from_brigade(*brigade_ref)
                if not result.ok:
                    logger.warning("Brigade bulk delete failed session=%s: %s", session_id, result.detail)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Brigade bulk delete failed session=%s: %s", session_id, exc)

    return {"deleted": deleted, "failed": failed}


class BulkExportRequest(BaseModel):
    session_ids: List[str]


def _safe_export_filename(session: DBRecordingSession) -> str:
    """Build a filesystem-safe ``<title>-<id>.md`` name for one session.

    Mirrors how the rest of the app titles a session (``title`` first, then
    legacy ``name``). The session id is always appended so two meetings that
    share a title never collide inside the archive, and the whole stem is
    sanitised down to ``[A-Za-z0-9._-]`` so the ZIP entry can't contain path
    separators or anything that would break a download on Windows.
    """
    title = (session.title or session.name or "meeting").strip()
    ident = session.session_id or str(session.id)
    stem = f"{title}-{ident}"
    # Collapse runs of unsafe chars to a single dash, trim the edges.
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-_.")
    if not safe:
        safe = f"meeting-{session.id}"
    # Keep the name well under any filesystem limit (room for the .md).
    return f"{safe[:120]}.md"


def _safe_download_filename(
    session: DBRecordingSession,
    descriptor: str,
    extension: str,
) -> str:
    """Return a header-safe filename for a single-session download."""
    stem = _safe_export_filename(session).removesuffix(".md")
    safe_descriptor = re.sub(r"[^A-Za-z0-9._-]+", "-", descriptor).strip("-_.")
    safe_extension = re.sub(r"[^A-Za-z0-9]+", "", extension).lower()
    return f"{stem}-{safe_descriptor}.{safe_extension}"


def _session_action_items_markdown(db: Session, session: DBRecordingSession) -> str:
    """Render the canonical ActionItem rows for one session as markdown.

    Action items are a first-class table (`action_items`) — the source of
    truth promoted out of `final_summary` — so we read them directly rather
    than re-parsing the summary JSON. Scoped by BOTH the session pk and the
    session's organization_id (defense in depth: the session was already
    org-resolved, this just makes the row query explicit).
    """
    from database.models import ActionItem

    items = (
        db.query(ActionItem)
        .filter(
            ActionItem.session_id == session.id,
            ActionItem.organization_id == session.organization_id,
        )
        .order_by(ActionItem.sort_order.asc(), ActionItem.id.asc())
        .all()
    )
    if not items:
        return ""

    lines: List[str] = ["", "## Action Items", ""]
    for item in items:
        checked = "x" if (item.status or "").lower() in ("done", "completed") else " "
        suffix: List[str] = []
        if item.owner:
            suffix.append(f"Owner: {item.owner}")
        if item.due_date:
            try:
                suffix.append(f"Due: {item.due_date.date().isoformat()}")
            except Exception:  # noqa: BLE001
                pass
        if item.status:
            suffix.append(f"Status: {item.status}")
        tail = f" ({', '.join(suffix)})" if suffix else ""
        lines.append(f"- [{checked}] {(item.text or '').strip()}{tail}")
    lines.append("")
    return "\n".join(lines)


def _build_session_export_markdown(db: Session, session: DBRecordingSession) -> str:
    """Full per-session markdown document for the bulk export.

    Reuses the canonical single-session renderer
    (`api.batch_export.export_to_markdown`) for the title / date / summary
    (`final_summary`) / transcript body, then layers on the two things the
    bulk-export spec wants that the shared renderer doesn't emit: the
    session **status** and the **action items from the ActionItem table**.
    """
    from api.batch_export import export_to_markdown, ExportOptions

    options = ExportOptions(
        includeTranscript=True,
        includeTimestamps=True,
        includeSpeakers=True,
    )
    body = export_to_markdown(session, options)

    # Inject a Status line right after the Date/Duration header block. The
    # shared renderer emits "**Date:** ...  " then "**Duration:** ..."; we
    # add status next to them rather than reformatting the document.
    status = session.status or "unknown"
    status_line = f"**Status:** {status}  "
    marker = "**Duration:**"
    idx = body.find(marker)
    if idx != -1:
        # Insert the status line just before the Duration line.
        line_start = body.rfind("\n", 0, idx) + 1
        body = body[:line_start] + status_line + "\n" + body[line_start:]
    else:
        # Fallback: prepend after the H1 title if the header drifted.
        body = body.rstrip() + "\n\n" + status_line + "\n"

    # Append the canonical action items (from the ActionItem table). The
    # shared renderer only knows the final_summary JSON copy; this is the
    # promoted, user-editable source of truth.
    body = body.rstrip() + "\n" + _session_action_items_markdown(db, session)
    return body.rstrip() + "\n"


@router.post("/recording-sessions/bulk-export")
async def bulk_export_sessions(
    request: BulkExportRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Export many sessions at once as a downloadable ZIP of markdown files.

    Same auth + org-scoping as bulk-delete: every id is resolved through
    `_get_session_for_org`, so only sessions in the caller's ACTIVE
    organization are exported. Ids that don't resolve in this org (missing,
    deleted, or belonging to another org) are skipped SILENTLY — we never
    leak cross-org existence and one bad id never aborts the batch.

    Each session becomes one markdown file (`<safe-title>-<id>.md`) holding
    its title, date, status, summary (`final_summary`), transcript, and the
    action items (from the `ActionItem` table). The archive is streamed
    (zipfile writing into a `BytesIO` we drain per entry) so memory stays
    bounded no matter how many sessions are requested.

    Defense-in-depth tier gate matches the batch-export surface: free users
    have no server-side canonical content to export (browser-only tier).

    Body:    { "session_ids": ["<uuid-or-int>", ...] }
    Returns: streaming ZIP, Content-Type application/zip,
             Content-Disposition attachment; filename=
             "meeting-ops-export-<YYYYMMDD>.zip"
    """
    import io
    import zipfile
    from fastapi.responses import StreamingResponse

    gate_feature_for_caller(current_user, "canonical_reprocess", active_org)

    org_id = active_org.organization.id

    # Resolve (and de-dup) ids up front so we don't hold the request body
    # open across the stream, and so an empty/all-cross-org request returns
    # a valid (empty) zip rather than erroring.
    sessions: List[DBRecordingSession] = []
    seen_ids: set[str] = set()
    for raw_id in request.session_ids:
        session_id = (raw_id or "").strip()
        if not session_id or session_id in seen_ids:
            continue
        seen_ids.add(session_id)
        session = _get_session_for_org(db, org_id, session_id, user=current_user)
        if session is None:
            # Silently skip ids outside the caller's active org.
            continue
        sessions.append(session)

    def _zip_stream():
        """Yield ZIP bytes one session at a time (bounded memory).

        We keep a single `BytesIO` + `ZipFile`, write one markdown entry,
        then drain whatever bytes the zip has flushed so far and truncate
        the buffer. Only one session's content is ever resident at once.
        """
        buffer = io.BytesIO()
        zf = zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED)
        used_names: set[str] = set()

        def _drain() -> bytes:
            data = buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            return data

        for session in sessions:
            try:
                markdown = _build_session_export_markdown(db, session)
            except Exception as exc:  # noqa: BLE001
                # Never let one bad session kill the whole download; emit a
                # stub file so the user knows it was attempted.
                logger.warning(
                    f"Bulk export: failed to render session "
                    f"{session.session_id or session.id}: {exc}"
                )
                markdown = (
                    f"# {session.title or session.name or 'Untitled Meeting'}\n\n"
                    f"Export failed for this session: {str(exc)[:200]}\n"
                )

            name = _safe_export_filename(session)
            # Guarantee uniqueness inside the archive even after sanitising.
            if name in used_names:
                base = name[:-3]  # strip ".md"
                name = f"{base}-{session.id}.md"
            used_names.add(name)

            zf.writestr(name, markdown)
            chunk = _drain()
            if chunk:
                yield chunk

        zf.close()
        tail = _drain()
        if tail:
            yield tail

    filename = f"meeting-ops-export-{datetime.now(timezone.utc).strftime('%Y%m%d')}.zip"
    return StreamingResponse(
        _zip_stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )

# Export endpoints for audio download and transcript download
@router.get("/recording-sessions/{session_id}/download/audio")
async def download_audio(
    session_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Download audio file for a session"""
    from fastapi.responses import FileResponse, JSONResponse
    
    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Resolve a local path: prefer the local working copy; if it's gone
    # (e.g. evicted), pull the durable copy back from Garage into the cache.
    # Backend proxies the bytes via FileResponse — Garage is never exposed
    # to the browser, and range requests (audio scrubbing) keep working.
    from services.session_media import resolve_local_path
    resolved = resolve_local_path(session)
    if resolved is None:
        if not session.audio_file:
            logger.warning(f"Session {session_id} has no audio file path")
            return JSONResponse(
                status_code=404,
                content={"detail": "No audio file associated with this session"}
            )
        logger.warning(f"Audio file not found locally or in Garage: {session.audio_file}")
        return JSONResponse(
            status_code=404,
            content={"detail": f"Audio file not found for session {session_id}"}
        )
    audio_path_str = str(resolved)
    # If the local working-copy path drifted, repoint it (cheap, idempotent).
    if session.audio_file != audio_path_str and resolved.exists():
        session.audio_file = audio_path_str
        try:
            db.commit()
        except Exception:
            db.rollback()

    filename = f"{(session.name or session.title or 'recording').replace(' ', '_')}_audio.wav"
    # Set headers for both streaming and download
    return FileResponse(
        audio_path_str,
        filename=filename, 
        media_type="audio/wav",
        headers={
            "Accept-Ranges": "bytes",  # Support range requests for audio streaming
            "Cache-Control": "no-cache"
        }
    )

@router.get("/recording-sessions/{session_id}/download/transcript")
async def download_transcript(
    session_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Download transcript with speakers for a session"""
    from fastapi.responses import Response
    
    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Build transcript text with speaker diarization
    transcript = f"Recording: {session.name or session.title or 'Untitled'}\n"
    transcript += f"Date: {session.created_at}\n"
    transcript += f"Duration: {session.duration or 0} seconds\n"
    
    # Add metadata if available
    if session.processing_metadata:
        transcript += f"Word Count: {session.processing_metadata.get('word_count', 'N/A')}\n"
        transcript += f"Speakers: {session.processing_metadata.get('speaker_count', 'N/A')}\n"
    
    transcript += "\n--- TRANSCRIPT WITH SPEAKERS ---\n\n"
    
    # Use diarized transcript if available
    if session.transcript_diarized and isinstance(session.transcript_diarized, dict):
        for segment in session.transcript_diarized.get("segments", []):
            speaker = segment.get("speaker", "Unknown Speaker")
            text = segment.get("text", "")
            timestamp = segment.get("start", 0)
            minutes = int(timestamp // 60)
            seconds = int(timestamp % 60)
            transcript += f"[{minutes:02d}:{seconds:02d} - {speaker}]: {text}\n\n"
    else:
        # Fallback to segments from Transcription table
        segments = db.query(Transcription).filter(
            Transcription.session_id == session.id
        ).order_by(Transcription.start_time).all()
        
        if segments:
            for segment in segments:
                speaker = segment.speaker or "Unknown Speaker"
                transcript += f"[{speaker}]: {segment.text}\n\n"
        elif session.transcript:
            try:
                trans_data = json.loads(session.transcript) if isinstance(session.transcript, str) else session.transcript
                if isinstance(trans_data, dict) and "segments" in trans_data:
                    for seg in trans_data["segments"]:
                        speaker = seg.get("speaker", "Unknown Speaker")
                        text = seg.get("text", "")
                        transcript += f"[{speaker}]: {text}\n\n"
                else:
                    transcript += str(session.transcript)
            except (json.JSONDecodeError, TypeError, ValueError):
                transcript += str(session.transcript) if session.transcript else "No transcription available."
        else:
            transcript += "No transcription available."

    filename = f"{(session.name or session.title or 'recording').replace(' ', '_')}_transcript_with_speakers.txt"
    return Response(
        content=transcript,
        media_type="text/plain",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        }
    )

@router.get("/recording-sessions/{session_id}/download/summary/pdf")
async def download_summary_pdf(
    session_id: str,
    include_transcript: bool = False,
    brand_mode: Literal[
        "default", "meeting_ops", "workspace", "unbranded"
    ] = "default",
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Download a branded meeting report, optionally with a transcript appendix."""
    from fastapi.responses import Response

    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from api.batch_export import export_to_pdf, ExportOptions
        options = ExportOptions(
            includeTranscript=include_transcript,
            brandMode=brand_mode,
        )
        pdf_bytes = export_to_pdf(session, options)
        filename = _safe_download_filename(
            session,
            "report-with-transcript" if include_transcript else "report",
            "pdf",
        )
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"PDF generation not available: {e}")
    except Exception as e:
        logger.error(f"PDF generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

@router.get("/recording-sessions/{session_id}/download/summary/md")
async def download_summary_md(
    session_id: str,
    include_transcript: bool = False,
    brand_mode: Literal[
        "default", "meeting_ops", "workspace", "unbranded"
    ] = "default",
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Download meeting summary as GitHub-flavored markdown.

    Set ?include_transcript=true to append the full diarized transcript
    after the summary sections.
    """
    from fastapi.responses import Response

    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from api.batch_export import export_to_markdown, ExportOptions
        options = ExportOptions(
            includeTranscript=include_transcript,
            brandMode=brand_mode,
        )
        md = export_to_markdown(session, options)
        filename = _safe_download_filename(
            session,
            "report-with-transcript" if include_transcript else "report",
            "md",
        )
        return Response(
            content=md,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception as e:
        logger.error(f"Markdown generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Markdown generation failed: {e}")


@router.get("/recording-sessions/{session_id}/download/summary/docx")
async def download_summary_docx(
    session_id: str,
    include_transcript: bool = False,
    brand_mode: Literal[
        "default", "meeting_ops", "workspace", "unbranded"
    ] = "default",
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Download a branded meeting report, optionally with a transcript appendix."""
    from fastapi.responses import Response

    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from api.batch_export import export_to_docx, ExportOptions
        options = ExportOptions(
            includeTranscript=include_transcript,
            brandMode=brand_mode,
        )
        docx_bytes = export_to_docx(session, options)
        filename = _safe_download_filename(
            session,
            "report-with-transcript" if include_transcript else "report",
            "docx",
        )
        return Response(
            content=docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"DOCX generation not available: {e}")
    except Exception as e:
        logger.error(f"DOCX generation failed: {e}")
        raise HTTPException(status_code=500, detail=f"DOCX generation failed: {str(e)}")

@router.get("/recording-sessions/{session_id}/download/transcript/simple")
async def download_transcript_simple(
    session_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Download simple transcript without speakers"""
    from fastapi.responses import Response
    
    session = _get_session_for_org(db, active_org.organization.id, session_id, user=current_user)
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Build simple transcript text
    transcript = f"Recording: {session.name or session.title or 'Untitled'}\n"
    transcript += f"Date: {session.created_at}\n"
    transcript += f"Duration: {session.duration or 0} seconds\n"
    
    if session.processing_metadata:
        transcript += f"Word Count: {session.processing_metadata.get('word_count', 'N/A')}\n"
    
    transcript += "\n--- TRANSCRIPT ---\n\n"
    
    # Use simple transcript if available
    if session.transcript_simple:
        transcript += session.transcript_simple
    else:
        # Fallback to extracting text from diarized transcript
        if session.transcript_diarized and isinstance(session.transcript_diarized, dict):
            texts = [seg.get("text", "") for seg in session.transcript_diarized.get("segments", [])]
            transcript += " ".join(texts)
        elif session.transcript:
            try:
                trans_data = json.loads(session.transcript) if isinstance(session.transcript, str) else session.transcript
                if isinstance(trans_data, dict) and "segments" in trans_data:
                    texts = [seg.get("text", "") for seg in trans_data["segments"]]
                    transcript += " ".join(texts)
                elif isinstance(trans_data, dict) and "text" in trans_data:
                    transcript += trans_data["text"]
                else:
                    transcript += str(session.transcript)
            except (json.JSONDecodeError, TypeError, ValueError):
                transcript += str(session.transcript) if session.transcript else "No transcription available."
        else:
            transcript += "No transcription available."

    filename = f"{(session.name or session.title or 'recording').replace(' ', '_')}_transcript_simple.txt"
    return Response(
        content=transcript,
        media_type="text/plain",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        }
    )


# === Storage Management ===

@router.get("/storage/stats")
async def get_storage_stats(
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Get storage usage statistics"""
    import glob
    from services.working_audio_service import audio_service as _as
    recordings_dir = _as.RECORDINGS_DIR

    # Count files and total size
    wav_files = glob.glob(os.path.join(recordings_dir, "**", "*.wav"), recursive=True)
    total_size = sum(os.path.getsize(f) for f in wav_files if os.path.exists(f))

    # Get session counts
    total_sessions = _scoped_session_query(db, active_org.organization.id).count()
    sessions_with_audio = _scoped_session_query(db, active_org.organization.id).filter(
        DBRecordingSession.audio_file.isnot(None)
    ).count()

    return {
        "recordings_dir": recordings_dir,
        "total_files": len(wav_files),
        "total_size_bytes": total_size,
        "total_size_mb": round(total_size / (1024 * 1024), 1),
        "total_sessions": total_sessions,
        "sessions_with_audio": sessions_with_audio
    }


@router.post("/storage/cleanup")
async def cleanup_old_recordings(
    days: int = Query(default=90, ge=1, le=365, description="Delete recordings older than N days"),
    dry_run: bool = Query(default=True, description="If true, only report what would be deleted"),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db)
):
    """Delete WAV files older than N days. Keeps transcripts and summaries."""

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # Find old sessions with audio files
    old_sessions = _scoped_session_query(db, active_org.organization.id).filter(
        DBRecordingSession.created_at < cutoff,
        DBRecordingSession.audio_file.isnot(None),
        DBRecordingSession.status.in_(["completed", "failed"])
    ).all()

    results = []
    freed_bytes = 0

    for session in old_sessions:
        audio_path = session.audio_file
        file_size = 0
        if audio_path and os.path.exists(audio_path):
            file_size = os.path.getsize(audio_path)
            freed_bytes += file_size

            if not dry_run:
                os.unlink(audio_path)
                session.audio_file = None
                # Keep everything else (transcript, summary, title, etc.)

        results.append({
            "session_id": session.session_id,
            "name": session.name,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "file_size_mb": round(file_size / (1024 * 1024), 1),
            "action": "deleted" if not dry_run else "would_delete"
        })

    if not dry_run:
        db.commit()

    return {
        "dry_run": dry_run,
        "cutoff_date": cutoff.isoformat(),
        "sessions_affected": len(results),
        "space_freed_mb": round(freed_bytes / (1024 * 1024), 1),
        "details": results
    }


# === Always-On Recording Mode ===

@router.post("/always-on/start")
async def start_always_on(
    body: Optional[StartRecordingRequest] = None,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Enable always-on recording mode. The system will listen continuously
    and automatically create meeting sessions when speech is detected."""
    # v3.29.2: always-on is server-side continuous capture + per-segment
    # processing — gate it like /start (was ungated). Non-invasive front-gate:
    # paid/internal pass straight through to the unchanged recorder; Free/Basic
    # get 403. Inert on the cloud node (no USB mic) so this can't regress it;
    # the org-scoping refactor of retroactive-session creation stays deferred
    # to a desktop/USB-mic test pass.
    gate_feature_for_caller(current_user, "canonical_reprocess", active_org)
    from services.always_on_recorder import always_on_recorder

    device_id = body.device_id if body else None
    result = await always_on_recorder.start(device_id=device_id)
    # Scope every auto-detected meeting to the requesting user's org. Without
    # this the recorder refuses to create sessions (organization_id is NOT
    # NULL) — see AlwaysOnRecorder.attach_owner / _start_new_meeting.
    always_on_recorder.attach_owner(active_org.organization.id, current_user.id)
    return result


@router.post("/always-on/stop")
async def stop_always_on(
    current_user: User = Depends(get_current_user),
):
    """Disable always-on recording mode"""
    from services.always_on_recorder import always_on_recorder

    result = await always_on_recorder.stop()
    return result


@router.get("/always-on/status")
async def get_always_on_status(
    current_user: User = Depends(get_current_user),
):
    """Get always-on recording status"""
    from services.always_on_recorder import always_on_recorder

    return always_on_recorder.get_status()
