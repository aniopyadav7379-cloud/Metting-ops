"""Per-session participants CRUD.

Participants are stored as a JSONB list on `recording_sessions.participants`.
Each entry is {"id": str (uuid4), "name": str, "email": str|null, "role": str|null}.

The `id` is generated server-side at insert and used for PATCH/DELETE addressing
so the frontend never has to reason about list indices (which shift under
concurrent edits). Order of the list is preserved as insertion order.
"""
from __future__ import annotations

import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import RecordingSession
from services import contact_ops_resolver


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/simple/recording-sessions/{session_id}/participants",
    tags=["session-participants"],
)


class ParticipantIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: Optional[EmailStr] = None
    role: Optional[str] = Field(default=None, max_length=100)
    # Contact-Ops person id (federation join key). Stamped when known
    # so the inbound Customer-Ops reads can find meetings by contact.
    contact_id: Optional[str] = Field(default=None, max_length=64)
    # Deprecated compatibility inputs. The server never persists client-supplied
    # match evidence for a manual selection; see _verified_contact_link.
    contact_match_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    contact_match_basis: Optional[str] = Field(default=None, max_length=40)


class ParticipantPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    email: Optional[EmailStr] = None
    role: Optional[str] = Field(default=None, max_length=100)
    contact_id: Optional[str] = Field(default=None, max_length=64)
    contact_match_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    contact_match_basis: Optional[str] = Field(default=None, max_length=40)


class ParticipantOut(BaseModel):
    id: str
    name: str
    email: Optional[str] = None
    role: Optional[str] = None
    contact_id: Optional[str] = None
    contact_match_confidence: Optional[float] = None
    contact_match_basis: Optional[str] = None
    contact_link_source: Optional[str] = None


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


def _can_edit(
    session: RecordingSession,
    user: User,
    active_org: ActiveOrganization,
) -> bool:
    """Org admin/manager, session creator, or superuser may mutate participants.
    Mirrors the auth check in api.session_emails.email_attendees so the two
    write surfaces stay in lockstep."""
    if getattr(user, "is_superuser", False):
        return True
    if active_org.role_name in {"owner", "admin", "manager"}:
        return True
    if session.user_id and session.user_id == user.id:
        return True
    return False


def _normalize(participants: object) -> List[dict]:
    """Defensive load — JSONB column should always be a list but a stray
    None or dict shouldn't 500 the endpoint."""
    if not participants:
        return []
    if isinstance(participants, list):
        return [p for p in participants if isinstance(p, dict)]
    return []


def _to_out(p: dict) -> ParticipantOut:
    return ParticipantOut(
        id=str(p.get("id") or ""),
        name=str(p.get("name") or ""),
        email=p.get("email") or None,
        role=p.get("role") or None,
        contact_id=p.get("contact_id") or None,
        contact_match_confidence=p.get("contact_match_confidence"),
        contact_match_basis=p.get("contact_match_basis") or None,
        contact_link_source=p.get("contact_link_source") or None,
    )


async def _verified_contact_link(
    *,
    contact_id: str,
    confidence: Optional[float],
    basis: Optional[str],
    workspace_id: str,
) -> dict:
    """Validate/reconcile a manual Contact-Ops link before persistence.

    ``confidence`` and ``basis`` are deliberately ignored. They are supplied
    by a browser search result and must not become integrity-bearing provenance
    just because a caller can forge a PATCH body. The Contact-Ops canonical
    resolution below remains the only trust decision; the resulting link is
    labelled as a manual selection.
    """

    _ = confidence, basis
    if not workspace_id:
        raise HTTPException(
            status_code=409,
            detail="Active organization is not mapped to a federation workspace",
        )
    if not contact_ops_resolver.is_configured():
        raise HTTPException(
            status_code=503,
            detail="Contact-Ops identity resolver is not configured",
        )
    resolution = await contact_ops_resolver.resolve_person_id(
        contact_id,
        workspace_id,
    )
    if resolution is None:
        raise HTTPException(
            status_code=503,
            detail="Contact-Ops identity resolution is unavailable",
        )
    canonical = resolution.get("canonical_person_id")
    if resolution.get("status") not in {"canonical", "redirected"} or not canonical:
        raise HTTPException(
            status_code=422,
            detail="Contact-Ops person is not linkable in this workspace",
        )
    return {
        "contact_id": str(canonical),
        "contact_match_confidence": None,
        "contact_match_basis": "manual_selection",
        "contact_link_source": "manual",
    }


@router.get("", response_model=List[ParticipantOut])
async def list_participants(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user)
    return [_to_out(p) for p in _normalize(session.participants)]


@router.post("", response_model=ParticipantOut, status_code=201)
async def add_participant(
    session_id: str,
    payload: ParticipantIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user, min_level="edit")
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(status_code=403, detail="Not permitted to edit participants")

    contact_fields = {
        "contact_id": None,
        "contact_match_confidence": None,
        "contact_match_basis": None,
        "contact_link_source": None,
    }
    if payload.contact_id:
        contact_fields = await _verified_contact_link(
            contact_id=payload.contact_id.strip(),
            confidence=payload.contact_match_confidence,
            basis=payload.contact_match_basis,
            workspace_id=(
                getattr(active_org.organization, "workspace_id", None) or ""
            ).strip(),
        )
    new = {
        "id": str(uuid.uuid4()),
        "name": payload.name.strip(),
        "email": (str(payload.email) if payload.email else None),
        "role": (payload.role.strip() if payload.role else None) or None,
        **contact_fields,
    }
    current = _normalize(session.participants)
    current.append(new)
    session.participants = current
    flag_modified(session, "participants")
    db.commit()
    logger.info("participants: add session=%s name=%s", session.id, new["name"])
    return _to_out(new)


@router.patch("/{participant_id}", response_model=ParticipantOut)
async def update_participant(
    session_id: str,
    participant_id: str,
    payload: ParticipantPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user, min_level="edit")
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(status_code=403, detail="Not permitted to edit participants")

    current = _normalize(session.participants)
    updated = None
    for p in current:
        if str(p.get("id")) == participant_id:
            if payload.name is not None:
                p["name"] = payload.name.strip()
            # Distinguish "not provided" from "explicitly cleared" via model_fields_set
            if "email" in payload.model_fields_set:
                p["email"] = str(payload.email) if payload.email else None
            if "role" in payload.model_fields_set:
                p["role"] = (payload.role.strip() if payload.role else None) or None
            if "contact_id" in payload.model_fields_set:
                if payload.contact_id:
                    p.update(
                        await _verified_contact_link(
                            contact_id=payload.contact_id.strip(),
                            confidence=payload.contact_match_confidence,
                            basis=payload.contact_match_basis,
                            workspace_id=(
                                getattr(
                                    active_org.organization,
                                    "workspace_id",
                                    None,
                                )
                                or ""
                            ).strip(),
                        )
                    )
                else:
                    p["contact_id"] = None
                    p["contact_match_confidence"] = None
                    p["contact_match_basis"] = None
                    p["contact_link_source"] = None
            updated = p
            break

    if updated is None:
        raise HTTPException(status_code=404, detail="Participant not found")

    session.participants = current
    flag_modified(session, "participants")
    db.commit()
    return _to_out(updated)


@router.delete("/{participant_id}", status_code=204)
async def delete_participant(
    session_id: str,
    participant_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    session = _resolve_session(db, active_org.organization.id, session_id, current_user, min_level="edit")
    if not _can_edit(session, current_user, active_org):
        raise HTTPException(status_code=403, detail="Not permitted to edit participants")

    current = _normalize(session.participants)
    remaining = [p for p in current if str(p.get("id")) != participant_id]
    if len(remaining) == len(current):
        raise HTTPException(status_code=404, detail="Participant not found")

    session.participants = remaining
    flag_modified(session, "participants")
    db.commit()
    return None
