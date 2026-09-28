"""Conference Room API — Phase 1.

Endpoints for creating and managing physical conference rooms, attaching
audio sources, issuing pairing codes, starting/stopping recordings, and
granting per-room access.

Every endpoint is org-scoped — the active organization comes from
``get_current_organization`` and EVERY query filters by it. ACL checks
augment org membership with per-room grants (admin/member/viewer). The
``can_access_room`` helper is the single authoritative authn/authz call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
import struct
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, desc
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.device_auth import generate_device_secret, hash_device_secret
from auth.models import User
from auth.organization import ActiveOrganization
from auth.tier import gate_feature_for_caller
from config.features import ROOM_MODE_ENABLED
from database.database import get_db
from database.models import RecordingSession, SatelliteDevice
from database.models_rooms import (
    ROOM_HARDWARE_TYPES,
    ROOM_ROLES,
    ROOM_STATUSES,
    ConferenceRoom,
    RoomAcl,
    RoomAudioSource,
    RoomPairingCode,
)


logger = logging.getLogger(__name__)


def _require_room_mode() -> None:
    """v3.18.3: kill-switch for Conference Room mode.

    Cloud / VPS deploys have no physical mic, so the room endpoints make no
    sense there. When ``ROOM_MODE_ENABLED=false`` every room endpoint short-
    circuits with 503 before any DB work or auth — including direct URLs
    typed by curious users.
    """
    if not ROOM_MODE_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Room mode is disabled on this deployment.",
        )


router = APIRouter(
    prefix="/api/rooms",
    tags=["rooms"],
    dependencies=[Depends(_require_room_mode)],
)
# Separate router for /api/system/audio-devices so it doesn't collide with
# the rooms prefix routing. Loaded alongside in main.py via router_attr.
system_router = APIRouter(
    prefix="/api/system",
    tags=["system"],
    dependencies=[Depends(_require_room_mode)],
)


# === Pydantic models ====================================================


class RoomCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    location: Optional[str] = Field(default=None, max_length=500)
    default_retention_days: int = Field(default=90, ge=1, le=3650)
    schedule_json: Optional[dict[str, Any]] = None
    legal_hold: bool = False


class RoomUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    location: Optional[str] = Field(default=None, max_length=500)
    status: Optional[str] = None
    default_retention_days: Optional[int] = Field(default=None, ge=1, le=3650)
    schedule_json: Optional[dict[str, Any]] = None
    legal_hold: Optional[bool] = None


class RoomResponse(BaseModel):
    id: str
    organization_id: int
    name: str
    location: Optional[str]
    status: str
    default_retention_days: int
    schedule_json: Optional[dict[str, Any]]
    legal_hold: bool
    created_at: str
    updated_at: str


class AudioSourceCreateRequest(BaseModel):
    hardware_type: str
    device_path: Optional[str] = Field(default=None, max_length=255)
    device_id: Optional[int] = None  # satellite_devices.id
    label: Optional[str] = Field(default=None, max_length=255)
    config_json: Optional[dict[str, Any]] = None


class AudioSourceResponse(BaseModel):
    id: str
    room_id: str
    hardware_type: str
    device_path: Optional[str]
    device_id: Optional[int]
    label: Optional[str]
    status: str
    config_json: Optional[dict[str, Any]]
    created_at: str


class PairingCodeResponse(BaseModel):
    id: str
    room_id: str
    code: str
    expires_at: str
    created_at: str


class PairingRedeemRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)
    # Phase 3 enrolment: the device provides its own device_id (string —
    # the value the device will use against
    # /ws/satellite/{device_id}/audio). If absent we fall back to the
    # legacy server-side flow used by USB-mic enrolment in Phase 1, which
    # does not need a device_secret because nothing on the host is
    # remotely-addressable as a "device".
    device_id: Optional[str] = None
    # Optional human-readable name + room hint the device may carry
    # forward from its provisioning step. Server is free to override.
    device_name: Optional[str] = Field(default=None, max_length=200)
    device_type: Optional[str] = Field(default=None, max_length=50)


class PairingRedeemResponse(BaseModel):
    room_id: str
    redeemed_at: str
    # Phase 3: the issued device record + the PLAINTEXT device secret. The
    # plaintext is returned exactly once — devices store it locally and
    # cannot retrieve it from the server. Both fields are absent on the
    # legacy host-side redemption path (USB mic, Phase 1) where no
    # satellite device is involved.
    device_id: Optional[str] = None
    organization_id: Optional[int] = None
    device_secret: Optional[str] = None
    secret_warning: Optional[str] = None


class AclGrantRequest(BaseModel):
    user_id: int
    role: str


class AclResponse(BaseModel):
    id: str
    room_id: str
    user_id: int
    role: str
    created_at: str


class RecordingStartResponse(BaseModel):
    session_id: str
    session_pk: int
    room_id: str
    source_id: Optional[str]
    started_at: str


class RecordingStopResponse(BaseModel):
    session_id: str
    session_pk: int
    stopped_at: str


class AudioDeviceInfo(BaseModel):
    card: int
    device: int
    card_name: str
    card_label: str
    device_name: str
    device_label: str
    device_path: str  # hw:X,Y
    is_usb: bool


class AudioDeviceProbeResponse(BaseModel):
    """One-shot mic level probe: record N seconds and return RMS+peak.

    Values are dBFS — 0 dB = clipping, -90 dB = nominal silence floor.
    The frontend renders these as a VU bar.
    """
    device_path: str
    duration_sec: float
    sample_count: int
    rms_db: float
    peak_db: float
    detected_audio: bool


class RoomActiveSessionResponse(BaseModel):
    """Quick lookup for "which session is currently recording in this room"
    so the Live tab can poll its transcript without a separate sessions
    list scan."""
    room_id: str
    session_id: Optional[str] = None
    session_pk: Optional[int] = None
    started_at: Optional[str] = None
    status: Optional[str] = None


# === Conversion helpers =================================================


def _room_to_response(room: ConferenceRoom) -> RoomResponse:
    return RoomResponse(
        id=str(room.id),
        organization_id=room.organization_id,
        name=room.name,
        location=room.location,
        status=room.status,
        default_retention_days=room.default_retention_days,
        schedule_json=room.schedule_json,
        legal_hold=bool(room.legal_hold),
        created_at=room.created_at.isoformat() if room.created_at else "",
        updated_at=room.updated_at.isoformat() if room.updated_at else "",
    )


def _source_to_response(source: RoomAudioSource) -> AudioSourceResponse:
    return AudioSourceResponse(
        id=str(source.id),
        room_id=str(source.room_id),
        hardware_type=source.hardware_type,
        device_path=source.device_path,
        device_id=source.device_id,
        label=source.label,
        status=source.status,
        config_json=source.config_json,
        created_at=source.created_at.isoformat() if source.created_at else "",
    )


def _pairing_to_response(pairing: RoomPairingCode) -> PairingCodeResponse:
    return PairingCodeResponse(
        id=str(pairing.id),
        room_id=str(pairing.room_id),
        code=pairing.code,
        expires_at=pairing.expires_at.isoformat() if pairing.expires_at else "",
        created_at=pairing.created_at.isoformat() if pairing.created_at else "",
    )


def _acl_to_response(acl: RoomAcl) -> AclResponse:
    return AclResponse(
        id=str(acl.id),
        room_id=str(acl.room_id),
        user_id=acl.user_id,
        role=acl.role,
        created_at=acl.created_at.isoformat() if acl.created_at else "",
    )


# === Org + ACL helpers ==================================================


def _is_org_admin(active_org: ActiveOrganization) -> bool:
    """Org admin / superuser bypass for room-level admin operations."""
    return active_org.role in {"admin", "superuser"}


def _get_room_or_404(
    db: Session,
    room_id: str,
    organization_id: int,
) -> ConferenceRoom:
    """Look up a room within the caller's org. Cross-org reads are
    indistinguishable from non-existent rooms — never reveal existence."""
    try:
        room_uuid = uuid.UUID(room_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Room not found")
    room = (
        db.query(ConferenceRoom)
        .filter(
            ConferenceRoom.id == room_uuid,
            ConferenceRoom.organization_id == organization_id,
        )
        .first()
    )
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    return room


def can_access_room(
    db: Session,
    user: User,
    room: ConferenceRoom,
    active_org: ActiveOrganization,
    required_role: str = "viewer",
) -> bool:
    """Return True if `user` can access `room` at >= `required_role`.

    * Superusers and org admins always pass.
    * Otherwise the user must have a row in `room_acl` for this room
      whose role is >= required_role.

    Role precedence: admin > member > viewer. Unknown role strings (e.g. a
    future `legal_officer`) are treated as equivalent to "member" for
    access-tier purposes; the API enforces specific role names at write
    time.
    """
    if user.is_superuser or _is_org_admin(active_org):
        return True

    # Cross-org safety: room must belong to caller's active org.
    if room.organization_id != active_org.organization.id:
        return False

    acl = (
        db.query(RoomAcl)
        .filter(
            RoomAcl.room_id == room.id,
            RoomAcl.user_id == user.id,
        )
        .first()
    )
    if not acl:
        return False

    precedence = {"viewer": 0, "member": 1, "admin": 2}
    return precedence.get(acl.role, precedence["member"]) >= precedence.get(
        required_role, 0
    )


def _require_org_admin(active_org: ActiveOrganization) -> None:
    if not _is_org_admin(active_org):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Org admin required",
        )


def _require_room_role(
    db: Session,
    user: User,
    room: ConferenceRoom,
    active_org: ActiveOrganization,
    required_role: str,
) -> None:
    if not can_access_room(db, user, room, active_org, required_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient access to room (required: {required_role})",
        )


# === Room CRUD ==========================================================


@router.post("", response_model=RoomResponse)
async def create_room(
    request: RoomCreateRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Create a conference room. Admin-only."""
    _require_org_admin(active_org)

    room = ConferenceRoom(
        organization_id=active_org.organization.id,
        name=request.name,
        location=request.location,
        default_retention_days=request.default_retention_days,
        schedule_json=request.schedule_json,
        legal_hold=request.legal_hold,
    )
    db.add(room)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Most likely a unique-constraint violation on (org_id, name).
        raise HTTPException(
            status_code=409,
            detail=f"Room name '{request.name}' is already in use in this organization",
        )
    db.refresh(room)
    logger.info(
        "Created conference room %s (org=%s, name=%r)",
        room.id,
        room.organization_id,
        room.name,
    )
    return _room_to_response(room)


@router.get("", response_model=list[RoomResponse])
async def list_rooms(
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """List rooms in the caller's active organization."""
    rooms = (
        db.query(ConferenceRoom)
        .filter(ConferenceRoom.organization_id == active_org.organization.id)
        .order_by(desc(ConferenceRoom.created_at))
        .all()
    )
    return [_room_to_response(r) for r in rooms]


@router.get("/{room_id}", response_model=RoomResponse)
async def get_room(
    room_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Get a room's detail. Requires viewer-or-better access."""
    room = _get_room_or_404(db, room_id, active_org.organization.id)
    _require_room_role(db, current_user, room, active_org, "viewer")
    return _room_to_response(room)


@router.put("/{room_id}", response_model=RoomResponse)
async def update_room(
    room_id: str,
    request: RoomUpdateRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Update room metadata. Admin-only."""
    _require_org_admin(active_org)
    room = _get_room_or_404(db, room_id, active_org.organization.id)

    if request.name is not None:
        room.name = request.name
    if request.location is not None:
        room.location = request.location
    if request.status is not None:
        if request.status not in ROOM_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status. Allowed: {sorted(ROOM_STATUSES)}",
            )
        room.status = request.status
    if request.default_retention_days is not None:
        room.default_retention_days = request.default_retention_days
    if request.schedule_json is not None:
        room.schedule_json = request.schedule_json
    if request.legal_hold is not None:
        room.legal_hold = request.legal_hold

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Update failed (likely a duplicate name in this organization)",
        )
    db.refresh(room)
    return _room_to_response(room)


@router.delete("/{room_id}")
async def delete_room(
    room_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Delete a room. Admin-only. Cascades to audio sources, pairing codes,
    and ACL rows; sets room_id NULL on historical recording sessions."""
    _require_org_admin(active_org)
    room = _get_room_or_404(db, room_id, active_org.organization.id)
    db.delete(room)
    db.commit()
    return {"deleted": True, "room_id": room_id}


# === Audio sources ======================================================


@router.post("/{room_id}/sources", response_model=AudioSourceResponse)
async def add_audio_source(
    room_id: str,
    request: AudioSourceCreateRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Attach an audio source to a room. Admin-only.

    Validates hardware_type against the known set. For ``server_usb_mic``
    sources `device_path` must look like an ALSA hw spec (`hw:X,Y` or
    `plughw:X,Y`). Other types defer validation to the recorder.
    """
    _require_org_admin(active_org)
    room = _get_room_or_404(db, room_id, active_org.organization.id)

    if request.hardware_type not in ROOM_HARDWARE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid hardware_type. Allowed: {sorted(ROOM_HARDWARE_TYPES)}",
        )

    if request.hardware_type == "server_usb_mic":
        if not request.device_path:
            raise HTTPException(
                status_code=400,
                detail="server_usb_mic requires device_path (e.g. hw:2,0)",
            )
        # Loose validation — allow hw:X,Y / plughw:X,Y / sysdefault and the
        # `default` alias. The recorder fails loudly if it can't open the
        # device, which is the real source of truth.
        if not re.match(r"^(plug)?hw:\d+(,\d+)?$|^default$|^sysdefault$", request.device_path):
            raise HTTPException(
                status_code=400,
                detail="device_path must be hw:X[,Y], plughw:X[,Y], default, or sysdefault",
            )

    source = RoomAudioSource(
        room_id=room.id,
        hardware_type=request.hardware_type,
        device_path=request.device_path,
        device_id=request.device_id,
        label=request.label,
        config_json=request.config_json,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return _source_to_response(source)


@router.get("/{room_id}/sources", response_model=list[AudioSourceResponse])
async def list_audio_sources(
    room_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """List audio sources for a room. Viewer-or-better."""
    room = _get_room_or_404(db, room_id, active_org.organization.id)
    _require_room_role(db, current_user, room, active_org, "viewer")
    sources = (
        db.query(RoomAudioSource)
        .filter(RoomAudioSource.room_id == room.id)
        .order_by(RoomAudioSource.created_at)
        .all()
    )
    return [_source_to_response(s) for s in sources]


@router.delete("/{room_id}/sources/{source_id}")
async def remove_audio_source(
    room_id: str,
    source_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Remove a source from a room. Admin-only."""
    _require_org_admin(active_org)
    room = _get_room_or_404(db, room_id, active_org.organization.id)
    try:
        source_uuid = uuid.UUID(source_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail="Source not found")
    source = (
        db.query(RoomAudioSource)
        .filter(
            RoomAudioSource.id == source_uuid,
            RoomAudioSource.room_id == room.id,
        )
        .first()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    db.delete(source)
    db.commit()
    return {"deleted": True, "source_id": source_id}


# === Pairing codes ======================================================


PAIRING_TTL = timedelta(minutes=10)
PAIRING_MAX_GENERATION_ATTEMPTS = 20


def _generate_unique_pairing_code(db: Session) -> str:
    """Cryptographically random 6-digit numeric code. Unique across all
    currently-active (not redeemed, not expired) pairing codes — once
    redeemed/expired, the value is free to be reused for a different room.
    Retries up to PAIRING_MAX_GENERATION_ATTEMPTS times before giving up
    (with 10**6 code space and short TTL collisions are vanishingly rare).
    """
    now = datetime.now(timezone.utc)
    for _ in range(PAIRING_MAX_GENERATION_ATTEMPTS):
        # secrets.randbelow is cryptographically strong; zero-pad to width 6.
        candidate = f"{secrets.randbelow(1_000_000):06d}"
        active_collision = (
            db.query(RoomPairingCode)
            .filter(
                RoomPairingCode.code == candidate,
                RoomPairingCode.redeemed_at.is_(None),
                RoomPairingCode.expires_at > now,
            )
            .first()
        )
        if not active_collision:
            return candidate
    raise HTTPException(
        status_code=503,
        detail="Could not generate a unique pairing code; try again",
    )


@router.post("/{room_id}/pairing-codes", response_model=PairingCodeResponse)
async def generate_pairing_code(
    room_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Generate a 6-digit pairing code with 10-min TTL. Admin-only.

    Codes are unique across all currently-active codes (system-wide); once
    redeemed or expired, the value can be reissued for a different room.
    """
    _require_org_admin(active_org)
    room = _get_room_or_404(db, room_id, active_org.organization.id)

    now = datetime.now(timezone.utc)
    code = _generate_unique_pairing_code(db)
    pairing = RoomPairingCode(
        room_id=room.id,
        code=code,
        created_at=now,
        expires_at=now + PAIRING_TTL,
    )
    db.add(pairing)
    db.commit()
    db.refresh(pairing)
    logger.info(
        "Generated pairing code for room %s (org=%s, ttl=%ss)",
        room.id,
        room.organization_id,
        int(PAIRING_TTL.total_seconds()),
    )
    return _pairing_to_response(pairing)


@router.post("/pairing-codes/redeem", response_model=PairingRedeemResponse)
async def redeem_pairing_code(
    request: PairingRedeemRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Redeem a pairing code, marking it consumed.

    Two flows are supported:

    * **Legacy (Phase 1)** — no ``device_id`` in the request. Used by
      USB-mic enrolment from the host. The code is marked consumed; no
      satellite device row is created and no secret is issued. This
      preserves backward compat for the admin UI's Phase 1 flow.

    * **Phase 3 satellite** — ``device_id`` (string identifier the
      device will use on /ws/satellite/{device_id}/audio) is in the
      request. We create or look up the SatelliteDevice row in this
      org, generate a fresh cryptographically-random ``device_secret``,
      bcrypt-hash it, and persist it on the device row. The plaintext
      secret is returned in this response **exactly once** — devices
      store it locally and can never retrieve it again. Losing the
      secret means re-pairing.

    Org scoping: a code generated in org A cannot be redeemed from
    org B's session — the join through ConferenceRoom enforces this.
    """
    code = request.code.strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="code must be 6 digits")

    now = datetime.now(timezone.utc)
    # Join through the room to enforce org scoping. A leaked code from
    # another org's admin will not redeem from this org's API session.
    pairing = (
        db.query(RoomPairingCode)
        .join(ConferenceRoom, ConferenceRoom.id == RoomPairingCode.room_id)
        .filter(
            RoomPairingCode.code == code,
            RoomPairingCode.redeemed_at.is_(None),
            RoomPairingCode.expires_at > now,
            ConferenceRoom.organization_id == active_org.organization.id,
        )
        .first()
    )
    if not pairing:
        # Don't tell the caller which case it is — a generic 404 prevents
        # brute-force discovery of valid codes from other orgs.
        raise HTTPException(status_code=404, detail="Code is invalid or expired")

    org_id = active_org.organization.id

    issued_secret: Optional[str] = None
    device_row: Optional[SatelliteDevice] = None

    if request.device_id is not None:
        device_id_str = request.device_id.strip()
        if not device_id_str:
            raise HTTPException(
                status_code=400,
                detail="device_id must be a non-empty string",
            )

        # Look up existing device (must belong to this org), else create.
        # device_id is globally unique at the column level — refuse to
        # let another org's device "redeem" into this org.
        device_row = db.query(SatelliteDevice).filter(
            SatelliteDevice.device_id == device_id_str
        ).first()

        if device_row is not None and device_row.organization_id not in (
            None,
            org_id,
        ):
            # Cross-org collision: the same device_id is already bound to
            # a different org. Refuse — never let a redemption move a
            # device between orgs.
            raise HTTPException(
                status_code=409,
                detail="Device is already registered to another organization",
            )

        # Generate the fresh secret + hash before any DB mutation so we
        # never persist a half-state.
        issued_secret = generate_device_secret()
        hashed = hash_device_secret(issued_secret)

        if device_row is None:
            device_row = SatelliteDevice(
                device_id=device_id_str,
                name=request.device_name or device_id_str,
                device_type=request.device_type or "satellite",
                organization_id=org_id,
                status="online",
                last_heartbeat=now,
                device_secret=hashed,
                created_at=now,
            )
            db.add(device_row)
            db.flush()  # populate device_row.id before linking
        else:
            # Re-pair: rotate the secret on the existing row. Any
            # previously-issued secret is now invalid.
            device_row.device_secret = hashed
            device_row.organization_id = org_id  # idempotent — was already org_id
            if request.device_name:
                device_row.name = request.device_name
            if request.device_type:
                device_row.device_type = request.device_type

        pairing.redeemed_by_device_id = device_row.id
        # Attach the device's "room_name" hint so list_rooms surfaces
        # this device under the room it just paired into.
        room_row = (
            db.query(ConferenceRoom)
            .filter(ConferenceRoom.id == pairing.room_id)
            .first()
        )
        if room_row and not device_row.room_name:
            device_row.room_name = room_row.name

    pairing.redeemed_at = now
    db.commit()
    db.refresh(pairing)

    response = PairingRedeemResponse(
        room_id=str(pairing.room_id),
        redeemed_at=pairing.redeemed_at.isoformat(),
    )
    if device_row is not None and issued_secret is not None:
        response.device_id = device_row.device_id
        response.organization_id = org_id
        response.device_secret = issued_secret
        response.secret_warning = (
            "Save this device_secret securely. It is shown exactly once "
            "and cannot be retrieved later. Re-pair this device if lost."
        )

        # Audit log — record that a secret was issued, never the secret
        # itself. The plaintext is in the response body and nowhere else
        # on the server.
        logger.info(
            "Issued device_secret for satellite device_id=%s (org=%s, room=%s)",
            device_row.device_id,
            org_id,
            pairing.room_id,
        )

    return response


# === Recording lifecycle ================================================


@router.post("/{room_id}/recordings/start", response_model=RecordingStartResponse)
async def start_recording(
    room_id: str,
    source_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Start a recording on this room. Requires member-or-better.

    Creates a RecordingSession row pinned to this room (and optionally a
    specific source). If `source_id` is omitted the room's first
    configured source is used. The RoomRecorder is started in the
    background — the response returns immediately with the new session id.
    """
    room = _get_room_or_404(db, room_id, active_org.organization.id)
    _require_room_role(db, current_user, room, active_org, "member")

    # v3.18.1 tier gate: room recording spawns RoomRecorder which then
    # posts chunks via InternalServiceCaller (legitimately bypassing the
    # gate once started). Gate at start so free orgs cannot kick off the
    # server pipeline at all.
    gate_feature_for_caller(current_user, "canonical_reprocess", active_org)

    if room.status == "recording":
        raise HTTPException(
            status_code=409,
            detail="Room is already recording",
        )

    # Resolve source.
    source: Optional[RoomAudioSource] = None
    if source_id is not None:
        try:
            source_uuid = uuid.UUID(source_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=404, detail="Source not found")
        source = (
            db.query(RoomAudioSource)
            .filter(
                RoomAudioSource.id == source_uuid,
                RoomAudioSource.room_id == room.id,
            )
            .first()
        )
        if not source:
            raise HTTPException(status_code=404, detail="Source not found")
    else:
        source = (
            db.query(RoomAudioSource)
            .filter(RoomAudioSource.room_id == room.id)
            .order_by(RoomAudioSource.created_at)
            .first()
        )
        if not source:
            raise HTTPException(
                status_code=400,
                detail="Room has no audio sources; add one before starting a recording",
            )

    started_at = datetime.now(timezone.utc)
    title = f"{room.name} - {started_at.strftime('%Y-%m-%d %H:%M')}"
    session = RecordingSession(
        session_id=str(uuid.uuid4()),
        name=title,
        title=title,
        description=f"Conference room recording: {room.name}",
        meeting_type="conference_room",
        mode="always_on",  # uses the always-on chunk pipeline
        created_at=started_at,
        started_at=started_at,
        status="recording",
        duration=0.0,
        user_id=current_user.id,
        organization_id=active_org.organization.id,
        source_type="room-server-usb-mic"
        if source.hardware_type == "server_usb_mic"
        else "room-other",
        room_name=room.name,
        room_id=room.id,
        room_source_id=source.id,
        processing_metadata={
            "source_label": source.label or source.device_path or source.hardware_type,
            "created_by": "conference_room",
            "room_id": str(room.id),
            "source_id": str(source.id),
            "hardware_type": source.hardware_type,
        },
    )
    db.add(session)

    room.status = "recording"
    source.status = "recording"
    db.commit()
    db.refresh(session)

    # Kick off the recorder. Import locally so test fixtures can patch the
    # module without affecting the API import graph.
    try:
        from services.room_recorder import start_room_recorder

        await start_room_recorder(room=room, source=source, session=session)
    except Exception as exc:
        # Recorder failed to start — roll the room/source back to error so
        # the UI shows it and the admin can investigate.
        logger.exception(
            "Failed to start RoomRecorder for room %s source %s: %s",
            room.id,
            source.id,
            exc,
        )
        room.status = "error"
        source.status = "error"
        session.status = "error"
        db.commit()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start recorder: {exc}",
        )

    return RecordingStartResponse(
        session_id=session.session_id or str(session.id),
        session_pk=session.id,
        room_id=str(room.id),
        source_id=str(source.id),
        started_at=started_at.isoformat(),
    )


@router.post("/{room_id}/recordings/stop", response_model=RecordingStopResponse)
async def stop_recording(
    room_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Stop the current recording on this room. Member-or-better.

    Finds the active RecordingSession for this room, terminates the
    RoomRecorder subprocess, and marks the session/room/source idle.
    """
    room = _get_room_or_404(db, room_id, active_org.organization.id)
    _require_room_role(db, current_user, room, active_org, "member")

    session = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.room_id == room.id,
            RecordingSession.organization_id == active_org.organization.id,
            RecordingSession.status.in_(("recording", "processing", "paused")),
        )
        .order_by(desc(RecordingSession.created_at))
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="No active recording on this room")

    stopped_at = datetime.now(timezone.utc)

    try:
        from services.room_recorder import stop_room_recorder

        await stop_room_recorder(room_id=room.id)
    except Exception:
        # Best-effort stop. We still flip the DB to a sane state so the UI
        # isn't stuck on "recording" forever.
        logger.exception("RoomRecorder stop failed for room %s", room.id)

    session.status = "completed"
    session.ended_at = stopped_at
    if session.started_at:
        # SQLAlchemy returns DateTime columns as naive in SQLite; treat
        # them as UTC for the arithmetic. Postgres returns aware values
        # natively. Coerce both sides to aware-UTC before subtracting.
        started = session.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        delta = stopped_at - started
        # Don't overwrite duration if the chunk pipeline already accumulated it.
        if not session.duration or session.duration <= 0:
            session.duration = max(0.0, delta.total_seconds())
    room.status = "idle"
    if session.room_source_id:
        source = (
            db.query(RoomAudioSource)
            .filter(RoomAudioSource.id == session.room_source_id)
            .first()
        )
        if source:
            source.status = "idle"
    db.commit()
    db.refresh(session)

    return RecordingStopResponse(
        session_id=session.session_id or str(session.id),
        session_pk=session.id,
        stopped_at=stopped_at.isoformat(),
    )


# === ACL =================================================================


@router.post("/{room_id}/acl", response_model=AclResponse)
async def grant_acl(
    room_id: str,
    request: AclGrantRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Grant a user access to a room. Admin-only.

    `role` is intentionally a plain string so new roles (legal_officer,
    observer, etc.) can be added at the API layer without a schema
    migration. We require the role to be in ROOM_ROLES at write time;
    extend that set as needed.
    """
    _require_org_admin(active_org)
    room = _get_room_or_404(db, room_id, active_org.organization.id)

    if request.role not in ROOM_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Allowed: {sorted(ROOM_ROLES)}",
        )

    # Confirm the user belongs to the same org. We don't reveal users from
    # other orgs (returns 404 either way).
    from auth.models import UserOrganization

    membership = (
        db.query(UserOrganization)
        .filter(
            UserOrganization.user_id == request.user_id,
            UserOrganization.organization_id == active_org.organization.id,
        )
        .first()
    )
    if not membership:
        raise HTTPException(
            status_code=404,
            detail="User is not a member of this organization",
        )

    # Upsert: if a row exists, update role; otherwise insert.
    existing = (
        db.query(RoomAcl)
        .filter(
            RoomAcl.room_id == room.id,
            RoomAcl.user_id == request.user_id,
        )
        .first()
    )
    if existing:
        existing.role = request.role
        db.commit()
        db.refresh(existing)
        return _acl_to_response(existing)

    acl = RoomAcl(room_id=room.id, user_id=request.user_id, role=request.role)
    db.add(acl)
    db.commit()
    db.refresh(acl)
    return _acl_to_response(acl)


@router.delete("/{room_id}/acl/{user_id}")
async def revoke_acl(
    room_id: str,
    user_id: int,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Revoke a user's room ACL row. Admin-only."""
    _require_org_admin(active_org)
    room = _get_room_or_404(db, room_id, active_org.organization.id)

    acl = (
        db.query(RoomAcl)
        .filter(RoomAcl.room_id == room.id, RoomAcl.user_id == user_id)
        .first()
    )
    if not acl:
        raise HTTPException(status_code=404, detail="ACL row not found")
    db.delete(acl)
    db.commit()
    return {"deleted": True, "room_id": room_id, "user_id": user_id}


# === System: audio devices ==============================================


def _parse_arecord_l(output: str) -> list[AudioDeviceInfo]:
    """Parse the output of `arecord -l` into AudioDeviceInfo rows.

    arecord(1) emits each card+device combo on a single line in the
    shape::

        card 2: USB [Some USB Card], device 0: USB Audio [USB Audio]

    Some packages split the card and device clauses across lines (the
    device-line is then indented). We match both shapes by searching for
    "card ..." and "device ..." anywhere on the line, then pairing them.
    """
    devices: list[AudioDeviceInfo] = []
    card_pattern = re.compile(r"card\s+(\d+):\s+(\S+)\s+\[([^\]]+)\]")
    device_pattern = re.compile(
        r"device\s+(\d+):\s+([^\[]+?)\s+\[([^\]]+)\]"
    )
    current_card: Optional[dict[str, Any]] = None
    for raw_line in output.splitlines():
        card_match = card_pattern.search(raw_line)
        if card_match:
            current_card = {
                "card": int(card_match.group(1)),
                "card_name": card_match.group(2),
                "card_label": card_match.group(3),
            }
        device_match = device_pattern.search(raw_line)
        if device_match and current_card is not None:
            card = current_card["card"]
            dev = int(device_match.group(1))
            dev_name = device_match.group(2).strip()
            dev_label = device_match.group(3)
            is_usb = (
                "USB" in current_card["card_label"]
                or "USB" in dev_label
                or "usb" in current_card["card_name"].lower()
            )
            devices.append(
                AudioDeviceInfo(
                    card=card,
                    device=dev,
                    card_name=current_card["card_name"],
                    card_label=current_card["card_label"],
                    device_name=dev_name,
                    device_label=dev_label,
                    device_path=f"hw:{card},{dev}",
                    is_usb=is_usb,
                )
            )
    return devices


@system_router.get("/audio-devices", response_model=list[AudioDeviceInfo])
async def list_audio_devices(
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Enumerate `arecord -l` capture devices on the backend container.

    Admin-only — only org admins or superusers see hardware. Used by the
    Conference Room setup wizard to populate the device picker.

    Returns an empty list if `arecord` is unavailable (container missing
    alsa-utils, or the host doesn't expose ALSA). Callers should treat
    empty as "no USB mics detected" rather than as an error.
    """
    _require_org_admin(active_org)
    try:
        proc = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        logger.warning("arecord binary not found — install alsa-utils in the backend image")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("arecord -l timed out after 5s")
        return []
    except Exception as exc:
        logger.warning("arecord -l failed: %s", exc)
        return []

    # `arecord -l` returns 0 even when no cards are present (with stderr
    # "no soundcards found"). Treat any non-zero exit as "no devices".
    if proc.returncode != 0:
        logger.info("arecord -l returned %s: %s", proc.returncode, proc.stderr.strip())
        return []
    return _parse_arecord_l(proc.stdout)


# === System: audio device probe (mic test + standalone VU) ==============


# Hard cap on probe duration. The endpoint blocks for `duration_sec`, so a
# rogue caller could otherwise hold a worker hostage. 10 s is plenty for a
# "say a sentence" check and small enough not to matter.
PROBE_DURATION_MAX_SEC = 10.0
PROBE_DURATION_MIN_SEC = 0.2
# dBFS floor (matches services.room_recorder.LEVEL_DB_FLOOR).
_PROBE_DB_FLOOR = -90.0
# Anything quieter than -55 dBFS is treated as "no audio detected" — that
# covers the noise floor of an open-but-disconnected USB mic.
_PROBE_DETECT_THRESHOLD_DB = -55.0


def _probe_db_from_pcm(buf: bytes) -> tuple[float, float, int]:
    """Compute (rms_db, peak_db, sample_count) for a raw S16_LE buffer.

    Mirrors the math in services.room_recorder._pcm_s16le_stats but lives
    here so the probe endpoint doesn't take a hard import dependency on
    the recorder (the recorder can be uninstalled / patched out in tests).
    """
    if not buf or len(buf) < 2:
        return _PROBE_DB_FLOOR, _PROBE_DB_FLOOR, 0
    try:
        import numpy as np

        samples = np.frombuffer(buf, dtype=np.int16)
        if samples.size == 0:
            return _PROBE_DB_FLOOR, _PROBE_DB_FLOOR, 0
        x = samples.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(np.square(x))))
        peak = float(np.max(np.abs(x)))
        n = int(samples.size)
    except ImportError:
        n = len(buf) // 2
        if n == 0:
            return _PROBE_DB_FLOOR, _PROBE_DB_FLOOR, 0
        samples = struct.unpack(f"<{n}h", buf[: n * 2])
        sq = 0.0
        peak_i = 0
        for s in samples:
            sq += float(s) * float(s)
            ai = -s if s < 0 else s
            if ai > peak_i:
                peak_i = ai
        rms = (sq / n) ** 0.5 / 32768.0
        peak = peak_i / 32768.0

    rms_db = 20.0 * math.log10(rms) if rms > 1e-9 else _PROBE_DB_FLOOR
    peak_db = 20.0 * math.log10(peak) if peak > 1e-9 else _PROBE_DB_FLOOR
    return max(rms_db, _PROBE_DB_FLOOR), max(peak_db, _PROBE_DB_FLOOR), n


def _validate_probe_device_path(device_path: str) -> str:
    """Validate `device_path` against the live `arecord -L` output.

    `arecord -L` returns every PCM the system knows about — a strict
    superset of `arecord -l`. This is what we match against so users can
    pick `plughw:` aliases without us hardcoding them. If `arecord -L` is
    unavailable, fall back to the same regex used at source-add time so
    the probe still works in test fixtures.
    """
    # Normalize. We never trust user input even after validation — the
    # cleaned value is what gets passed to subprocess as a -D arg.
    cleaned = device_path.strip()
    if not cleaned or "\n" in cleaned or "\r" in cleaned or ";" in cleaned:
        raise HTTPException(status_code=400, detail="Invalid device_path")
    # Loose shape-check is the floor; the arecord -L round-trip is the
    # real safety check.
    if not re.match(
        r"^(plug)?hw:\d+(,\d+)?$|^default$|^sysdefault$|^[A-Za-z0-9_:,=.\-]+$",
        cleaned,
    ):
        raise HTTPException(status_code=400, detail="Invalid device_path")

    try:
        proc = subprocess.run(
            ["arecord", "-L"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        # arecord absent — let the probe attempt fail loudly downstream.
        return cleaned
    except (subprocess.TimeoutExpired, Exception) as exc:  # noqa: BLE001
        logger.warning("arecord -L lookup failed during probe validation: %s", exc)
        return cleaned

    # arecord -L emits PCM names at column 0 and descriptive text indented.
    # Pull every non-indented line as a candidate name.
    known: set[str] = set()
    for raw in proc.stdout.splitlines():
        if raw and not raw.startswith(" ") and not raw.startswith("\t"):
            known.add(raw.strip())
    # `hw:X,Y` from `arecord -l` is always valid even when -L only lists
    # the parent name (some systems collapse to `hw:CARD=X,DEV=Y`).
    if cleaned in known:
        return cleaned
    if re.match(r"^(plug)?hw:\d+(,\d+)?$", cleaned):
        return cleaned
    raise HTTPException(
        status_code=400,
        detail=f"Device {cleaned!r} not found in arecord -L output",
    )


async def _probe_device_record(device_path: str, duration_sec: float) -> bytes:
    """Spawn arecord, capture `duration_sec` of raw S16_LE 16k mono, return bytes.

    Uses an explicit timeout — even if arecord refuses to exit cleanly
    (USB unplugged mid-capture) we terminate it aggressively so the
    request doesn't hang the worker.
    """
    cmd = [
        "arecord",
        "-D",
        device_path,
        "-f",
        "S16_LE",
        "-r",
        "16000",
        "-c",
        "1",
        "-t",
        "raw",
        "-d",
        f"{max(1, int(round(duration_sec)))}",
        "-q",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    # Allow a small slack on top of the recording window for arecord
    # startup overhead.
    hard_timeout = duration_sec + 3.0
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=hard_timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        raise HTTPException(
            status_code=504,
            detail="Audio probe timed out — the device may be in use.",
        )
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="ignore")[:300]
        raise HTTPException(
            status_code=502,
            detail=f"arecord failed (rc={proc.returncode}): {err}",
        )
    return stdout or b""


@system_router.get(
    "/audio-devices/{device_path:path}/test",
    response_model=AudioDeviceProbeResponse,
)
async def probe_audio_device(
    device_path: str,
    duration_sec: float = 3.0,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """One-shot mic level probe.

    Records `duration_sec` of audio from `device_path` (an ALSA name like
    ``hw:2,0`` — URL-encode any commas) and returns RMS + peak in dBFS.
    Used by the Room setup wizard "Test mic" button and as a polling
    fallback for the Live tab VU meter when SSE isn't available.

    Admin-only because it enumerates server hardware and starts a
    subprocess; the standard org-membership check happens via
    ``get_current_organization``.
    """
    _require_org_admin(active_org)

    if not isinstance(duration_sec, (int, float)) or math.isnan(float(duration_sec)):
        raise HTTPException(status_code=400, detail="duration_sec must be numeric")
    duration_sec = float(duration_sec)
    if duration_sec < PROBE_DURATION_MIN_SEC or duration_sec > PROBE_DURATION_MAX_SEC:
        raise HTTPException(
            status_code=400,
            detail=(
                f"duration_sec must be between {PROBE_DURATION_MIN_SEC}"
                f" and {PROBE_DURATION_MAX_SEC}"
            ),
        )

    validated_path = _validate_probe_device_path(device_path)
    raw = await _probe_device_record(validated_path, duration_sec)
    rms_db, peak_db, samples = _probe_db_from_pcm(raw)
    detected = peak_db > _PROBE_DETECT_THRESHOLD_DB

    return AudioDeviceProbeResponse(
        device_path=validated_path,
        duration_sec=duration_sec,
        sample_count=samples,
        rms_db=rms_db,
        peak_db=peak_db,
        detected_audio=detected,
    )


# === Live room audio levels (SSE) =======================================


# How often to emit a level frame while streaming. 100 ms matches the
# RoomDetail Live tab's VU bar refresh — anything faster wastes CPU,
# anything slower looks choppy.
_LEVEL_STREAM_INTERVAL_SEC = 0.1
# Maximum lifetime of a single SSE connection. Browsers will auto-
# reconnect; this just stops an idle tab from holding a worker forever.
_LEVEL_STREAM_MAX_SECONDS = 30 * 60
# Keepalive cadence — proxies happily drop a stream that's silent for
# too long. Comment frames are ignored by EventSource clients.
_LEVEL_STREAM_KEEPALIVE_SEC = 15.0


@router.get("/{room_id}/levels")
async def stream_room_levels(
    room_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Server-Sent Events stream of audio levels for an active room recording.

    Each event payload is ``{rms_db, peak_db, timestamp}`` (all dBFS) at
    ~10 Hz. When the recorder shuts down, the stream emits a final
    ``{event: "end"}`` and closes.

    The recorder ships an in-memory level tap so this endpoint never
    touches the audio buffer directly — it just snapshots the latest
    reading every 100 ms.

    Auth: room viewer-or-better. Cross-org reads return 404.
    """
    room = _get_room_or_404(db, room_id, active_org.organization.id)
    _require_room_role(db, current_user, room, active_org, "viewer")

    # Best-effort grab the recorder. If none is active we still emit a
    # short stream of silence frames so the UI doesn't spin on "connecting".
    try:
        from services.room_recorder import get_active_recorders
    except Exception:  # noqa: BLE001
        get_active_recorders = lambda: {}  # type: ignore[assignment]

    async def event_stream():
        started = datetime.now(timezone.utc)
        last_keepalive = started
        first = True
        while True:
            if await request.is_disconnected():
                return
            now = datetime.now(timezone.utc)
            if (now - started).total_seconds() >= _LEVEL_STREAM_MAX_SECONDS:
                yield (
                    "event: end\n"
                    f"data: {json.dumps({'reason': 'max-lifetime'})}\n\n"
                )
                return

            recorder = get_active_recorders().get(room.id)
            if recorder is None:
                # Either the room never started, or it just stopped.
                # First tick: emit a silence frame so the EventSource
                # opens cleanly. Subsequent ticks: shut down so the
                # client knows to stop polling.
                if first:
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "rms_db": _PROBE_DB_FLOOR,
                                "peak_db": _PROBE_DB_FLOOR,
                                "timestamp": now.isoformat(),
                                "active": False,
                            }
                        )
                        + "\n\n"
                    )
                    first = False
                    await asyncio.sleep(_LEVEL_STREAM_INTERVAL_SEC)
                    continue
                yield (
                    "event: end\n"
                    f"data: {json.dumps({'reason': 'recorder-stopped'})}\n\n"
                )
                return

            levels = recorder.get_levels()
            payload = {
                "rms_db": levels.get("rms_db", _PROBE_DB_FLOOR),
                "peak_db": levels.get("peak_db", _PROBE_DB_FLOOR),
                "timestamp": levels.get("timestamp") or now.isoformat(),
                "active": True,
            }
            yield "data: " + json.dumps(payload) + "\n\n"
            first = False

            if (now - last_keepalive).total_seconds() >= _LEVEL_STREAM_KEEPALIVE_SEC:
                yield ": keepalive\n\n"
                last_keepalive = now
            await asyncio.sleep(_LEVEL_STREAM_INTERVAL_SEC)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx/traefik buffering
        },
    )


# === Active session lookup ==============================================


@router.get(
    "/{room_id}/active-session",
    response_model=RoomActiveSessionResponse,
)
async def get_active_session(
    room_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
):
    """Return the currently-recording session for this room, if any.

    The Live tab uses this to discover the public session id to poll
    transcripts against. Returns ``session_id: null`` when the room is
    idle — that's a 200, not a 404, so the frontend can treat it as a
    steady-state. Viewer-or-better.
    """
    room = _get_room_or_404(db, room_id, active_org.organization.id)
    _require_room_role(db, current_user, room, active_org, "viewer")

    session = (
        db.query(RecordingSession)
        .filter(
            RecordingSession.room_id == room.id,
            RecordingSession.organization_id == active_org.organization.id,
            RecordingSession.status.in_(("recording", "processing", "paused")),
        )
        .order_by(desc(RecordingSession.created_at))
        .first()
    )
    if not session:
        return RoomActiveSessionResponse(room_id=str(room.id))
    return RoomActiveSessionResponse(
        room_id=str(room.id),
        session_id=session.session_id or str(session.id),
        session_pk=session.id,
        started_at=session.started_at.isoformat() if session.started_at else None,
        status=session.status,
    )
