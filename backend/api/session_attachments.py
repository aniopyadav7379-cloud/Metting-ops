"""Session-attachments CRUD scoped to the active organization.

Lets users attach files to a recording session: Granola notes from a
coworker, an external Otter/Zoom transcript, a slide deck, a PDF agenda,
a photo of a whiteboard, etc. Stored either in Garage (preferred when
``GARAGE_*`` env vars are set) or on local disk under
``RECORDINGS_DIR/attachments`` — the writer auto-selects and records
which one per-row, so a mid-deploy storage swap doesn't break old
attachments.

All endpoints enforce org scoping via ``_resolve_session`` (the same
helper shape used by ``api.sessions_participants`` and ``api.sessions_tags``).
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid as _uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import RecordingSession, SessionAttachment
from services import attachment_storage


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/simple/recording-sessions/{session_id}/attachments",
    tags=["session-attachments"],
)


# 100 MB per file — documented in the task spec and matched here.
# If you bump this, also bump the frontend dropzone hint copy.
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024

# Loose enum the UI uses to filter + pick an icon. Adding new values
# does NOT require a migration — the column is just a String(50).
ALLOWED_TYPES = {
    "transcript",
    "notes",
    "document",
    "audio",
    "image",
    "video",
    "other",
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AttachmentOut(BaseModel):
    id: str
    session_id: int
    filename: str
    mime_type: Optional[str] = None
    size_bytes: int
    attachment_type: str
    source_label: Optional[str] = None
    notes: Optional[str] = None
    uploaded_by_user_id: Optional[int] = None
    uploaded_by_username: Optional[str] = None
    created_at: Optional[str] = None
    storage_backend: str


class AttachmentPatch(BaseModel):
    attachment_type: Optional[str] = Field(default=None, max_length=50)
    source_label: Optional[str] = Field(default=None, max_length=200)
    notes: Optional[str] = Field(default=None, max_length=10_000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_session(
    db: Session,
    organization_id: int,
    session_id: str,
    user: User,
    min_level: str = "view",
) -> RecordingSession:
    """UUID-or-integer-pk lookup in the active org, with the canonical
    cross-org has_session_access fallback (see session_permissions)."""
    from api.session_permissions import resolve_session_for_user

    rec = resolve_session_for_user(db, organization_id, session_id, user, min_level)
    if not rec:
        raise HTTPException(status_code=404, detail="Session not found")
    return rec
    try:
        pk = int(session_id)
    except (TypeError, ValueError):
        pk = None
    if pk is not None:
        rec = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.id == pk,
                RecordingSession.organization_id == organization_id,
            )
            .first()
        )
    if not rec:
        raise HTTPException(status_code=404, detail="Session not found")
    return rec


def _can_edit(
    session: RecordingSession,
    user: User,
    active_org: ActiveOrganization,
) -> bool:
    """Same shape as the participants endpoint — org admin/manager,
    session creator, or superuser."""
    if getattr(user, "is_superuser", False):
        return True
    if active_org.role_name in {"owner", "admin", "manager"}:
        return True
    if session.user_id and session.user_id == user.id:
        return True
    return False


def _can_delete(
    session: RecordingSession,
    attachment: SessionAttachment,
    user: User,
    active_org: ActiveOrganization,
) -> bool:
    """Uploader or session admin may delete (matches task spec)."""
    if _can_edit(session, user, active_org):
        return True
    if (
        attachment.uploaded_by_user_id
        and attachment.uploaded_by_user_id == user.id
    ):
        return True
    return False


def _classify_default(filename: str, mime_type: Optional[str]) -> str:
    """Default attachment_type from filename + mime when the client
    didn't supply one. Frontend always sends it today, but a curl-only
    caller benefits from a sensible fallback."""
    name = (filename or "").lower()
    mt = (mime_type or "").lower()
    if mt.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if mt.startswith("audio/") or name.endswith((".mp3", ".wav", ".m4a", ".ogg", ".flac")):
        return "audio"
    if mt.startswith("video/") or name.endswith((".mp4", ".mov", ".mkv", ".webm")):
        return "video"
    if name.endswith((".txt", ".md", ".markdown")):
        # heuristic: short text files paired with the word "transcript" in
        # the source_label end up as transcripts; otherwise notes.
        return "notes"
    if name.endswith((".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv")):
        return "document"
    return "other"


def _to_out(att: SessionAttachment, *, uploader_username: Optional[str]) -> AttachmentOut:
    return AttachmentOut(
        id=str(att.id),
        session_id=int(att.session_id),
        filename=att.filename,
        mime_type=att.mime_type,
        size_bytes=int(att.size_bytes or 0),
        attachment_type=att.attachment_type or "other",
        source_label=att.source_label,
        notes=att.notes,
        uploaded_by_user_id=att.uploaded_by_user_id,
        uploaded_by_username=uploader_username,
        created_at=att.created_at.isoformat() if att.created_at else None,
        storage_backend=att.storage_backend or "local",
    )


def _username_for(db: Session, user_id: Optional[int]) -> Optional[str]:
    if not user_id:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    return user.username if user else None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=List[AttachmentOut])
async def list_attachments(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user)
    rows = (
        db.query(SessionAttachment)
        .filter(
            SessionAttachment.session_id == session.id,
            SessionAttachment.organization_id == session.organization_id,
        )
        .order_by(SessionAttachment.created_at.desc())
        .all()
    )
    # Resolve uploader usernames in one pass (small N).
    uploader_ids = {r.uploaded_by_user_id for r in rows if r.uploaded_by_user_id}
    username_by_id: dict[int, str] = {}
    if uploader_ids:
        for u in db.query(User).filter(User.id.in_(uploader_ids)).all():
            username_by_id[u.id] = u.username
    return [
        _to_out(r, uploader_username=username_by_id.get(r.uploaded_by_user_id))
        for r in rows
    ]


@router.post("", response_model=AttachmentOut, status_code=201)
async def create_attachment(
    session_id: str,
    file: UploadFile = File(...),
    attachment_type: Optional[str] = Form(default=None),
    source_label: Optional[str] = Form(default=None),
    notes: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user, min_level="edit")
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(
            status_code=403, detail="Not permitted to attach files to this session"
        )

    # Type validation. We accept unknown labels too (the UI may have
    # added a new chip ahead of the backend) — but constrain to a
    # short string to keep DB rows small.
    requested_type = (attachment_type or "").strip().lower() or _classify_default(
        file.filename or "", file.content_type
    )
    if len(requested_type) > 50:
        raise HTTPException(
            status_code=400, detail="attachment_type too long (max 50 chars)"
        )

    filename = (file.filename or "attachment.bin").strip()[:500] or "attachment.bin"
    mime = (file.content_type or None)
    if mime and len(mime) > 200:
        mime = mime[:200]

    # Stream the upload into a tmpfile so we can:
    #   1. measure size accurately (Content-Length is advisory)
    #   2. fail BEFORE we touch Garage/local storage if it's too big
    #   3. allow the storage writer to rewind on backend fallback
    tmp = tempfile.NamedTemporaryFile(
        delete=False, prefix="meet_attach_", suffix=".upload"
    )
    bytes_written = 0
    try:
        try:
            while True:
                chunk = await file.read(1 * 1024 * 1024)  # 1 MiB
                if not chunk:
                    break
                bytes_written += len(chunk)
                if bytes_written > MAX_ATTACHMENT_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"Attachment too large. Max is "
                            f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB."
                        ),
                    )
                tmp.write(chunk)
        finally:
            tmp.close()

        if bytes_written == 0:
            raise HTTPException(status_code=400, detail="Empty upload rejected")

        # Build storage key and persist. Keyed by the SESSION's org so the
        # object lands (and is later found) next to its session regardless
        # of which org the uploader was active in.
        storage_key, _attach_uuid = attachment_storage.build_storage_key(
            org_id=session.organization_id,
            session_pk=session.id,
            filename=filename,
        )
        with open(tmp.name, "rb") as f:
            actual_backend = attachment_storage.write_stream(
                storage_key=storage_key,
                stream=f,
                content_type=mime,
            )

        # Persist DB row last so a storage write that fails doesn't
        # leave a dangling row pointing at nothing.
        row = SessionAttachment(
            id=str(_uuid.uuid4()),
            session_id=session.id,
            # Stamp the SESSION's org, not the uploader's active org — a
            # cross-org upload must stay queryable next to its session.
            organization_id=session.organization_id,
            uploaded_by_user_id=current_user.id,
            filename=filename,
            mime_type=mime,
            size_bytes=bytes_written,
            attachment_type=requested_type,
            source_label=(source_label.strip()[:200] if source_label else None) or None,
            notes=(notes.strip()[:10_000] if notes else None) or None,
            storage_backend=actual_backend,
            storage_key=storage_key,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        logger.info(
            "session_attachments: created session=%s id=%s backend=%s bytes=%d",
            session.id,
            row.id,
            actual_backend,
            bytes_written,
        )
        return _to_out(row, uploader_username=current_user.username)
    finally:
        # Best-effort tmpfile cleanup. NamedTemporaryFile(delete=False)
        # leaves us responsible.
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


@router.get("/{attachment_id}/download")
async def download_attachment(
    session_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user)
    row = (
        db.query(SessionAttachment)
        .filter(
            SessionAttachment.id == attachment_id,
            SessionAttachment.session_id == session.id,
            SessionAttachment.organization_id == session.organization_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")

    try:
        body = attachment_storage.open_stream(
            storage_backend=row.storage_backend,
            storage_key=row.storage_key,
        )
    except FileNotFoundError as e:
        logger.warning(
            "session_attachments: missing object for id=%s key=%s: %s",
            row.id,
            row.storage_key,
            e,
        )
        raise HTTPException(
            status_code=404, detail="Attachment file missing from storage"
        )
    except Exception as e:
        logger.error(
            "session_attachments: download failed id=%s key=%s: %s",
            row.id,
            row.storage_key,
            e,
        )
        raise HTTPException(status_code=500, detail="Download failed")

    # Yield in 1 MiB chunks so the client gets bytes flowing instead of
    # the whole file materializing in memory on the server.
    def _iter():
        try:
            while True:
                if hasattr(body, "read"):
                    chunk = body.read(1 * 1024 * 1024)
                else:
                    chunk = next(body, b"")
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                body.close()
            except Exception:
                pass

    media_type = row.mime_type or "application/octet-stream"
    headers = {
        # `attachment` so the browser presents Save-As rather than try
        # to render unknown types inline (some PDFs from external orgs
        # have been weaponized as XSS vectors; we don't want them to
        # execute in our origin).
        "Content-Disposition": (
            f'attachment; filename="{_safe_disposition(row.filename)}"'
        ),
        "Content-Length": str(row.size_bytes or 0),
        "X-Attachment-Id": str(row.id),
    }
    return StreamingResponse(_iter(), media_type=media_type, headers=headers)


def _safe_disposition(name: str) -> str:
    """RFC 6266 simple-quoted filename. Strip quotes/control chars."""
    safe = (name or "attachment").replace('"', "").replace("\r", "").replace("\n", "")
    return safe[:255] or "attachment"


@router.put("/{attachment_id}", response_model=AttachmentOut)
async def update_attachment_metadata(
    session_id: str,
    attachment_id: str,
    payload: AttachmentPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Edit only the human metadata — type/label/notes. File content
    itself is immutable (a re-upload creates a new attachment row)."""
    session = _resolve_session(db, active_org.organization.id, session_id, current_user, min_level="edit")
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(
            status_code=403, detail="Not permitted to edit attachments"
        )

    row = (
        db.query(SessionAttachment)
        .filter(
            SessionAttachment.id == attachment_id,
            SessionAttachment.session_id == session.id,
            SessionAttachment.organization_id == session.organization_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")

    if "attachment_type" in payload.model_fields_set and payload.attachment_type:
        v = payload.attachment_type.strip().lower()[:50]
        if v:
            row.attachment_type = v
    if "source_label" in payload.model_fields_set:
        v = (payload.source_label or "").strip()[:200]
        row.source_label = v or None
    if "notes" in payload.model_fields_set:
        v = (payload.notes or "").strip()[:10_000]
        row.notes = v or None

    db.commit()
    db.refresh(row)
    return _to_out(
        row, uploader_username=_username_for(db, row.uploaded_by_user_id)
    )


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(
    session_id: str,
    attachment_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user, min_level="edit")
    row = (
        db.query(SessionAttachment)
        .filter(
            SessionAttachment.id == attachment_id,
            SessionAttachment.session_id == session.id,
            SessionAttachment.organization_id == session.organization_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Attachment not found")

    if not _can_delete(session, row, current_user, active_org):
        raise HTTPException(
            status_code=403,
            detail="Only the uploader or a session admin can delete this attachment",
        )

    # Storage delete BEFORE DB delete so a half-successful chain leaves
    # us with an orphaned DB row pointing at empty storage (harmless,
    # caught by the next list call which 500s only on the missing-bytes
    # download path, not on list). The reverse ordering would leak bytes.
    attachment_storage.delete_object(
        storage_backend=row.storage_backend,
        storage_key=row.storage_key,
    )

    db.delete(row)
    db.commit()
    logger.info(
        "session_attachments: deleted id=%s session=%s by user=%s",
        attachment_id,
        session.id,
        current_user.id,
    )
    return None


# ---------------------------------------------------------------------------
# Counts surface (consumed by the sessions list page to show the paperclip)
# ---------------------------------------------------------------------------

counts_router = APIRouter(
    prefix="/api/simple/recording-sessions",
    tags=["session-attachments"],
)


@counts_router.get("-attachment-counts")
async def attachment_counts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Cheap per-session count lookup the Sessions list uses to show
    a paperclip icon. One row per session_id with at least one
    attachment."""
    from sqlalchemy import func as sa_func

    rows = (
        db.query(
            SessionAttachment.session_id,
            sa_func.count(SessionAttachment.id).label("n"),
        )
        .filter(
            # CTX-FIX attachment_counts 2026-07-28: this read `session.organization_id`,
            # but `session` is never defined here -- the body was copy-pasted from the
            # per-session handlers (which do `session = _resolve_session(...)` first) and
            # that line was dropped. This is a LIST endpoint with no session_id, so the
            # correct scope is the caller's active org. Every call raised
            # NameError -> 500 (1588 on dogfood, 1662 on prod in a week), so the
            # Sessions-list paperclip icon never worked.
            SessionAttachment.organization_id == active_org.organization.id
        )
        .group_by(SessionAttachment.session_id)
        .all()
    )
    return {str(sid): int(n) for sid, n in rows}
