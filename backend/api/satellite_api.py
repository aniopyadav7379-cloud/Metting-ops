"""
Satellite Device Management API
Handles registration, heartbeat, audio upload, and transcript upload
for remote satellite recording devices (ESP32-S3, Raspberry Pi, companion apps).

Endpoint auth taxonomy (task #85):

  * **User-auth only** — admin CRUD operations driven by the web UI:
      register, list, get, update, delete, rooms.
  * **Dual-auth** — endpoints a deployed satellite device hits AND that
    an admin may invoke from the UI for diagnostics/control:
      heartbeat, upload-audio, transcript, start-recording, stop-recording.
    These accept either:
        - ``Authorization: Bearer <device_secret>`` /
          ``X-Device-Secret: <device_secret>`` header (device flow), OR
        - The normal user+active_org cookie/JWT (admin flow).
    When the device-secret flow authenticates, the request is bound to
    that device's organization_id. When the user flow authenticates,
    the device must belong to the user's active org (existing behaviour).
"""

from fastapi import (
    APIRouter,
    HTTPException,
    Depends,
    Header,
    Request,
    UploadFile,
    File,
    Form,
    Query,
)
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import desc
import json
import logging
import os
import uuid
import secrets
import hashlib

from database.database import get_db
from database.models import SatelliteDevice, RecordingSession as DBRecordingSession, Transcription
from auth.dependencies import get_current_user, get_current_organization
from auth.tier import get_org_tier
from auth.device_auth import (
    DeviceAuthError,
    authenticate_device,
    device_auth_from_http,
)
from auth.organization import ActiveOrganization
from auth.models import User, Organization

router = APIRouter(prefix="/api/satellites", tags=["satellites"])
logger = logging.getLogger(__name__)

# Recordings directory (same as working_audio_service)
RECORDINGS_DIR = os.environ.get(
    "RECORDINGS_DIR",
    # default beside this backend, not an absolute path from one machine
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings"),
)


# === Pydantic Models ===

class SatelliteRegisterRequest(BaseModel):
    device_id: str
    name: Optional[str] = None
    room_name: Optional[str] = None
    device_type: Optional[str] = "esp32-s3"
    capabilities: Optional[Dict[str, Any]] = None
    firmware_version: Optional[str] = None
    ip_address: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class SatelliteUpdateRequest(BaseModel):
    name: Optional[str] = None
    room_name: Optional[str] = None
    device_type: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    firmware_version: Optional[str] = None
    ip_address: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


class SatelliteResponse(BaseModel):
    id: int
    device_id: str
    name: Optional[str] = None
    room_name: Optional[str] = None
    device_type: Optional[str] = None
    capabilities: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    last_heartbeat: Optional[str] = None
    status: str = "offline"
    firmware_version: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    current_session_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class HeartbeatRequest(BaseModel):
    status: Optional[str] = None
    battery_pct: Optional[int] = None
    wifi_rssi: Optional[int] = None
    free_sd_mb: Optional[int] = None


class TranscriptUploadRequest(BaseModel):
    transcript_text: str
    segments: Optional[List[Dict[str, Any]]] = None
    duration: Optional[float] = None
    recorded_at: Optional[str] = None
    audio_file_available: Optional[bool] = False
    room_name: Optional[str] = None
    session_name: Optional[str] = None


class RoomResponse(BaseModel):
    room_name: str
    devices: List[SatelliteResponse]
    active_recording: bool = False


# === Helper Functions ===

def _device_to_response(device: SatelliteDevice) -> SatelliteResponse:
    """Convert a SatelliteDevice ORM object to a response model."""
    capabilities = None
    if device.capabilities:
        try:
            capabilities = json.loads(device.capabilities) if isinstance(device.capabilities, str) else device.capabilities
        except (json.JSONDecodeError, TypeError):
            capabilities = None

    config = None
    if device.config:
        try:
            config = json.loads(device.config) if isinstance(device.config, str) else device.config
        except (json.JSONDecodeError, TypeError):
            config = None

    return SatelliteResponse(
        id=device.id,
        device_id=device.device_id,
        name=device.name,
        room_name=device.room_name,
        device_type=device.device_type,
        capabilities=capabilities,
        ip_address=device.ip_address,
        last_heartbeat=device.last_heartbeat.isoformat() if device.last_heartbeat else None,
        status=device.status or "offline",
        firmware_version=device.firmware_version,
        config=config,
        current_session_id=device.current_session_id,
        created_at=device.created_at.isoformat() if device.created_at else None,
        updated_at=device.updated_at.isoformat() if device.updated_at else None,
    )


def _hash_api_key(key: str) -> str:
    """Hash an API key for storage."""
    return hashlib.sha256(key.encode()).hexdigest()


def _scoped_device_query(db: Session, organization_id: int):
    """Base query for satellite devices scoped to one org."""
    return db.query(SatelliteDevice).filter(
        SatelliteDevice.organization_id == organization_id
    )


def _get_device_or_404(db: Session, device_id: str, organization_id: int) -> SatelliteDevice:
    """Get a satellite device by device_id within the given org, or raise 404.

    Devices belonging to other organizations are indistinguishable from
    non-existent devices — the lookup must never reveal existence across
    org boundaries.
    """
    device = _scoped_device_query(db, organization_id).filter(
        SatelliteDevice.device_id == device_id
    ).first()
    if not device:
        raise HTTPException(status_code=404, detail=f"Satellite device '{device_id}' not found")
    return device


async def _optional_active_org(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[ActiveOrganization]:
    """Best-effort user-flow auth: returns the user's active org if a valid
    user session is present, or ``None`` otherwise.

    This is the inverse of ``get_current_organization`` — that one raises
    401 on missing/invalid auth; this one swallows the error so the
    endpoint can decide between device flow (header present) and user
    flow (header absent). The actual decision lives in
    ``_resolve_dual_auth``.
    """
    from auth.dependencies import get_current_user_optional
    from auth.organization import resolve_active_organization

    # Re-drive get_current_user_optional manually since we already have
    # the Request object — calling it through Depends inside another
    # Depends would force every state-mutating endpoint to share its
    # signature.
    try:
        # Pull the OAuth2 token + API key headers the way OAuth2PasswordBearer would.
        auth_header = request.headers.get("Authorization", "")
        token: Optional[str] = None
        if auth_header.lower().startswith("bearer "):
            token = auth_header.split(None, 1)[1].strip() or None
        api_key = request.headers.get("X-API-Key") or None

        user = await get_current_user_optional(
            request=request,
            token=token,
            api_key=api_key,
            db=db,
        )
        if user is None or not user.is_active:
            return None
        return resolve_active_organization(db, request, user)
    except HTTPException:
        # resolve_active_organization may raise if the user has no org.
        # That's a user-flow auth failure; let the device flow handle it.
        return None
    except Exception as exc:
        logger.warning("Optional user-flow auth failed unexpectedly: %s", exc)
        return None


def _resolve_dual_auth(
    *,
    db: Session,
    device_id: str,
    presented_secret: Optional[str],
    active_org: Optional[ActiveOrganization],
) -> Tuple[SatelliteDevice, int]:
    """Dual-auth resolver for state-mutating satellite endpoints.

    Resolves either:
      * **Device flow** — caller presented a device secret (in
        ``Authorization: Bearer`` or ``X-Device-Secret``). We verify it
        via the rate-limited ``authenticate_device`` helper. The
        device's ``organization_id`` wins; we ignore whatever org the
        user session was bound to. This is the path a deployed satellite
        uses.
      * **User flow** — no device secret presented. The user must have a
        live session and an active org, AND the device must belong to
        that org. This is the path an admin uses from the UI for
        diagnostics or remote control.

    Both flows return ``(device, organization_id)``.

    A request that presents BOTH (a device secret AND a valid user
    session) uses the **device flow**. We never silently fall back from
    device-auth-failed to user-auth — that would let an attacker
    holding a user session probe device secrets while masquerading as
    the user.
    """
    if presented_secret is not None:
        result = authenticate_device(
            db=db,
            device_id=device_id,
            plaintext_secret=presented_secret,
        )
        return result.device, result.org_id

    # User flow — fall through to the standard org-scoped lookup.
    if active_org is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
        )
    org_id = active_org.organization.id
    device = _get_device_or_404(db, device_id, org_id)
    return device, org_id


def _create_satellite_session(
    db: Session,
    device: SatelliteDevice,
    source_type: str,
    name: Optional[str] = None,
    audio_file: Optional[str] = None,
) -> DBRecordingSession:
    """Create a recording session linked to a satellite device."""
    session_id = str(uuid.uuid4())
    session_name = name or f"{device.name or device.device_id} - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"

    db_session = DBRecordingSession(
        session_id=session_id,
        name=session_name,
        title=session_name,
        description=f"Recorded by satellite device: {device.device_id}",
        status="active",
        created_at=datetime.now(timezone.utc),
        duration=0.0,
        source_device_id=device.device_id,
        source_type=source_type,
        room_name=device.room_name,
        audio_file=audio_file,
        # Pin the recording session to the same org as the device. Without
        # this, downstream org-scoped readers (list/search/RAG) would not
        # find satellite-originated meetings.
        organization_id=device.organization_id,
    )
    db.add(db_session)
    db.commit()
    db.refresh(db_session)
    return db_session


def _gate_satellite_for_org(db: Session, org_id: int) -> None:
    """v3.18.1 tier gate: satellite uploads + transcripts run the server
    STT/diarization/summary pipeline. Free orgs cannot trigger that path.

    Satellite endpoints authenticate by device secret OR user session, then
    resolve to the owning org. We gate on the owning org's `plan` column
    (free/pro/enterprise) rather than a user tier — the device IS the caller.
    """
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if org is None:
        raise HTTPException(status_code=404, detail="device organization not found")
    if get_org_tier(org) == "free":
        raise HTTPException(
            status_code=403,
            detail=(
                "Satellite devices require Pro or Enterprise. "
                "Upgrade at /pricing to use satellite capture."
            ),
        )


# === Endpoints ===

@router.post("/register")
async def register_satellite(
    request: SatelliteRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Register a new satellite device. Returns an API key for the device.

    The device is bound to the calling user's active organization. The
    request body is NOT trusted to specify which org owns the device —
    that is always taken from the resolved ``active_org``.
    """
    org_id = active_org.organization.id

    # device_id uniqueness is enforced at the column level (unique=True),
    # so the check below is global by design. A device id collision across
    # orgs is rare in practice (esp32 MACs / pi serials), but if it ever
    # happens we still refuse the registration rather than letting two orgs
    # claim the same physical device.
    existing = db.query(SatelliteDevice).filter(
        SatelliteDevice.device_id == request.device_id
    ).first()

    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Device '{request.device_id}' is already registered"
        )

    # Generate an API key for the device
    raw_api_key = f"sat_{secrets.token_urlsafe(32)}"
    hashed_key = _hash_api_key(raw_api_key)

    device = SatelliteDevice(
        device_id=request.device_id,
        name=request.name or request.device_id,
        room_name=request.room_name,
        device_type=request.device_type,
        capabilities=json.dumps(request.capabilities) if request.capabilities else None,
        ip_address=request.ip_address,
        firmware_version=request.firmware_version,
        config=json.dumps(request.config) if request.config else None,
        status="online",
        last_heartbeat=datetime.now(timezone.utc),
        api_key=hashed_key,
        created_at=datetime.now(timezone.utc),
        organization_id=org_id,
    )
    db.add(device)
    db.commit()
    db.refresh(device)

    logger.info(
        f"Registered satellite device: {request.device_id} "
        f"(type={request.device_type}, org_id={org_id})"
    )

    response = _device_to_response(device)
    return {
        "device": response.model_dump(),
        "api_key": raw_api_key,
        "message": "Device registered successfully. Store the API key securely."
    }


@router.get("")
async def list_satellites(
    status_filter: Optional[str] = Query(None, alias="status"),
    room: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> List[SatelliteResponse]:
    """List satellite devices registered to the caller's active organization."""
    org_id = active_org.organization.id
    query = _scoped_device_query(db, org_id).order_by(desc(SatelliteDevice.created_at))

    if status_filter:
        query = query.filter(SatelliteDevice.status == status_filter)
    if room:
        query = query.filter(SatelliteDevice.room_name == room)

    # Auto-mark stale devices as offline (no heartbeat in 90 seconds).
    # Scoped to this org — never touch another tenant's device rows.
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=90)
    stale_devices = _scoped_device_query(db, org_id).filter(
        SatelliteDevice.status != "offline",
        SatelliteDevice.last_heartbeat < stale_cutoff,
    ).all()
    for d in stale_devices:
        d.status = "offline"
    if stale_devices:
        db.commit()

    devices = query.all()
    return [_device_to_response(d) for d in devices]


@router.get("/rooms")
async def list_rooms(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> List[RoomResponse]:
    """List rooms and their assigned satellite devices, scoped to the caller's org."""
    devices = (
        _scoped_device_query(db, active_org.organization.id)
        .order_by(SatelliteDevice.room_name)
        .all()
    )

    rooms: Dict[str, List[SatelliteDevice]] = {}
    for d in devices:
        room = d.room_name or "Unassigned"
        rooms.setdefault(room, []).append(d)

    result = []
    for room_name, room_devices in rooms.items():
        active = any(d.status == "recording" for d in room_devices)
        result.append(RoomResponse(
            room_name=room_name,
            devices=[_device_to_response(d) for d in room_devices],
            active_recording=active,
        ))
    return result


@router.get("/{device_id}")
async def get_satellite(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> SatelliteResponse:
    """Get details of a specific satellite device in the caller's org."""
    device = _get_device_or_404(db, device_id, active_org.organization.id)
    return _device_to_response(device)


@router.put("/{device_id}")
async def update_satellite(
    device_id: str,
    request: SatelliteUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> SatelliteResponse:
    """Update satellite device configuration."""
    device = _get_device_or_404(db, device_id, active_org.organization.id)

    if request.name is not None:
        device.name = request.name
    if request.room_name is not None:
        device.room_name = request.room_name
    if request.device_type is not None:
        device.device_type = request.device_type
    if request.capabilities is not None:
        device.capabilities = json.dumps(request.capabilities)
    if request.firmware_version is not None:
        device.firmware_version = request.firmware_version
    if request.ip_address is not None:
        device.ip_address = request.ip_address
    if request.config is not None:
        device.config = json.dumps(request.config)

    db.commit()
    db.refresh(device)
    logger.info(f"Updated satellite device: {device_id}")
    return _device_to_response(device)


@router.delete("/{device_id}")
async def delete_satellite(
    device_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Remove a satellite device."""
    device = _get_device_or_404(db, device_id, active_org.organization.id)
    db.delete(device)
    db.commit()
    logger.info(f"Deleted satellite device: {device_id}")
    return {"message": f"Device '{device_id}' removed successfully"}


@router.post("/{device_id}/heartbeat")
async def satellite_heartbeat(
    device_id: str,
    request: Optional[HeartbeatRequest] = None,
    db: Session = Depends(get_db),
    presented_secret: Optional[str] = Depends(device_auth_from_http),
    active_org: Optional[ActiveOrganization] = Depends(_optional_active_org),
):
    """Update heartbeat timestamp and optional status for a satellite device.

    Dual-auth: device flow (Bearer/X-Device-Secret) OR user flow.
    """
    device, _org_id = _resolve_dual_auth(
        db=db,
        device_id=device_id,
        presented_secret=presented_secret,
        active_org=active_org,
    )

    device.last_heartbeat = datetime.now(timezone.utc)

    # Update status if provided, otherwise set to "online" if currently offline
    if request and request.status:
        device.status = request.status
    elif device.status == "offline":
        device.status = "online"

    # Store telemetry in config JSON if provided
    if request and any([request.battery_pct, request.wifi_rssi, request.free_sd_mb]):
        try:
            config = json.loads(device.config) if device.config else {}
        except (json.JSONDecodeError, TypeError):
            config = {}
        telemetry = {}
        if request.battery_pct is not None:
            telemetry["battery_pct"] = request.battery_pct
        if request.wifi_rssi is not None:
            telemetry["wifi_rssi"] = request.wifi_rssi
        if request.free_sd_mb is not None:
            telemetry["free_sd_mb"] = request.free_sd_mb
        config["last_telemetry"] = telemetry
        device.config = json.dumps(config)

    db.commit()
    return {
        "device_id": device_id,
        "status": device.status,
        "timestamp": device.last_heartbeat.isoformat()
    }


@router.post("/{device_id}/upload-audio")
async def upload_audio(
    device_id: str,
    file: UploadFile = File(...),
    recorded_at: Optional[str] = Form(None),
    session_id: Optional[str] = Form(None),
    room_name: Optional[str] = Form(None),
    session_name: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    presented_secret: Optional[str] = Depends(device_auth_from_http),
    active_org: Optional[ActiveOrganization] = Depends(_optional_active_org),
):
    """Upload a WAV audio file from a satellite device (store-and-forward mode).

    Creates a recording session and triggers the transcription + AI pipeline.

    Dual-auth: device flow (Bearer/X-Device-Secret) OR user flow.
    """
    device, org_id = _resolve_dual_auth(
        db=db,
        device_id=device_id,
        presented_secret=presented_secret,
        active_org=active_org,
    )
    _gate_satellite_for_org(db, org_id)

    # Validate file type
    if file.content_type and "audio" not in file.content_type and "octet-stream" not in file.content_type:
        raise HTTPException(status_code=400, detail=f"Invalid file type: {file.content_type}. Expected audio file.")

    # Ensure recordings directory exists
    os.makedirs(RECORDINGS_DIR, exist_ok=True)

    # Generate filename and save
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"satellite_{device_id}_{timestamp}.wav"
    file_path = os.path.join(RECORDINGS_DIR, filename)

    try:
        contents = await file.read()
        with open(file_path, "wb") as f:
            f.write(contents)
        logger.info(f"Saved satellite audio: {file_path} ({len(contents)} bytes)")
    except Exception as e:
        logger.error(f"Failed to save satellite audio: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save audio file: {str(e)}")

    # Override room_name if provided in form data
    if room_name:
        device.room_name = room_name

    # Create or use existing recording session. Lookup is scoped to the
    # caller's org so a malicious upload can never attach audio to another
    # tenant's session row.
    if session_id:
        db_session = db.query(DBRecordingSession).filter(
            DBRecordingSession.session_id == session_id,
            DBRecordingSession.organization_id == org_id,
        ).first()
        if not db_session:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
        db_session.audio_file = file_path
        db_session.source_device_id = device.device_id
        db_session.source_type = "satellite_upload"
        db.commit()
    else:
        db_session = _create_satellite_session(
            db, device,
            source_type="satellite_upload",
            name=session_name,
            audio_file=file_path,
        )

    # Parse recorded_at if provided
    if recorded_at:
        try:
            db_session.started_at = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError:
            pass

    db_session.status = "processing"
    device.status = "uploading"
    db.commit()

    # Trigger background processing (transcription + AI)
    try:
        import asyncio
        from api.simple_recording_db import process_recording
        asyncio.create_task(
            process_recording(
                session_id=db_session.session_id,
                audio_file=file_path,
                db_session_id=db_session.id,
            )
        )
        logger.info(f"Triggered processing pipeline for satellite upload: {db_session.session_id}")
    except Exception as e:
        logger.error(f"Failed to trigger processing pipeline: {e}")
        # Don't fail the upload - the file is saved, processing can be retried

    return {
        "session_id": db_session.session_id,
        "status": "processing",
        "audio_file": file_path,
        "file_size": len(contents),
        "message": "Audio uploaded successfully, transcription queued"
    }


@router.post("/{device_id}/transcript")
async def upload_transcript(
    device_id: str,
    request: TranscriptUploadRequest,
    db: Session = Depends(get_db),
    presented_secret: Optional[str] = Depends(device_auth_from_http),
    active_org: Optional[ActiveOrganization] = Depends(_optional_active_org),
):
    """Upload a transcript from a satellite device running local STT (e.g., RPi with whisper.cpp).

    Creates a session with the transcript pre-populated and triggers LLM summarization.

    Dual-auth: device flow (Bearer/X-Device-Secret) OR user flow.
    """
    device, _org_id = _resolve_dual_auth(
        db=db,
        device_id=device_id,
        presented_secret=presented_secret,
        active_org=active_org,
    )
    _gate_satellite_for_org(db, _org_id)

    if request.room_name:
        device.room_name = request.room_name

    # Create recording session with transcript pre-populated
    db_session = _create_satellite_session(
        db, device,
        source_type="satellite_transcript",
        name=request.session_name,
    )

    # Store transcript
    db_session.transcript_simple = request.transcript_text
    db_session.transcript = json.dumps({
        "text": request.transcript_text,
        "segments": request.segments or [],
    })
    if request.segments:
        db_session.transcript_diarized = {"segments": request.segments}

    # Set timing info
    if request.recorded_at:
        try:
            db_session.started_at = datetime.fromisoformat(request.recorded_at.replace("Z", "+00:00"))
        except ValueError:
            pass
    if request.duration:
        db_session.duration = request.duration
        if db_session.started_at:
            db_session.ended_at = db_session.started_at + timedelta(seconds=request.duration)

    # Save individual transcript segments
    segment_count = 0
    if request.segments:
        for seg in request.segments:
            try:
                trans = Transcription(
                    session_id=db_session.id,
                    text=seg.get("text", ""),
                    speaker=seg.get("speaker"),
                    start_time=float(seg.get("start", 0)),
                    end_time=float(seg.get("end", 0)),
                    confidence=float(seg.get("confidence", 0.9)),
                )
                db.add(trans)
                segment_count += 1
            except Exception as e:
                logger.warning(f"Failed to save transcript segment: {e}")
                continue

    db_session.status = "processing"
    db.commit()

    logger.info(f"Received transcript from {device_id}: {len(request.transcript_text)} chars, {segment_count} segments")

    # Trigger LLM summarization (skip Whisper since we already have the transcript)
    try:
        import asyncio
        from services.unified_agent_service import unified_agent_service

        async def _run_summarization():
            """Run only the AI summarization step on the pre-existing transcript."""
            from database.database import SessionLocal
            import time

            processing_start = time.time()
            local_db = SessionLocal()
            try:
                session = local_db.query(DBRecordingSession).filter(
                    DBRecordingSession.id == db_session.id
                ).first()
                if not session:
                    return

                full_text = session.transcript_simple or ""
                if not full_text.strip():
                    session.status = "completed"
                    local_db.commit()
                    return

                try:
                    summary_result = await asyncio.wait_for(
                        unified_agent_service.analyze_meeting(
                            session_id=str(session.id),
                            transcript=full_text,
                        ),
                        timeout=180,
                    )

                    if summary_result and summary_result.get("success"):
                        analysis = summary_result.get("analysis", {})
                        session.final_summary = {
                            "executive": analysis.get("executive", ""),
                            "bullets": analysis.get("bullets", []),
                            "actions": analysis.get("actions", []),
                            "decisions": analysis.get("decisions", []),
                            "title": analysis.get("title", ""),
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        session.summary = json.dumps(analysis)
                        session.ai_insights = analysis

                        if isinstance(analysis, dict) and "title" in analysis:
                            new_title = analysis["title"]
                            if new_title and isinstance(new_title, str):
                                session.title = new_title[:200]
                except asyncio.TimeoutError:
                    logger.error(f"AI analysis timed out for satellite transcript session {session.session_id}")
                except Exception as e:
                    logger.error(f"AI analysis failed for satellite transcript: {e}")

                session.processing_metadata = {
                    "source": "satellite_transcript",
                    "device_id": device_id,
                    "processing_time_seconds": round(time.time() - processing_start, 2),
                    "word_count": len(full_text.split()),
                    "segment_count": segment_count,
                }
                session.status = "completed"

                # Promote action items into the first-class table. Best-effort.
                try:
                    from services.action_items_extractor import persist_action_items
                    persist_action_items(local_db, session)
                except Exception as exc:
                    logger.warning(f"action_items promotion failed for satellite session {session.id}: {exc}")

                local_db.commit()
            except Exception as e:
                logger.error(f"Summarization pipeline error: {e}")
                local_db.rollback()
            finally:
                local_db.close()

        asyncio.create_task(_run_summarization())
    except Exception as e:
        logger.error(f"Failed to trigger summarization: {e}")

    return {
        "session_id": db_session.session_id,
        "status": "processing",
        "segments_saved": segment_count,
        "message": "Transcript received, AI summarization queued"
    }


@router.post("/{device_id}/start-recording")
async def start_satellite_recording(
    device_id: str,
    db: Session = Depends(get_db),
    presented_secret: Optional[str] = Depends(device_auth_from_http),
    active_org: Optional[ActiveOrganization] = Depends(_optional_active_org),
):
    """Signal a satellite device to start recording.

    Creates a recording session on the server side and updates device status.
    The device polls its status or receives the command via WebSocket.

    Dual-auth: device flow (Bearer/X-Device-Secret) OR user flow.
    """
    device, _org_id = _resolve_dual_auth(
        db=db,
        device_id=device_id,
        presented_secret=presented_secret,
        active_org=active_org,
    )

    if device.status == "recording":
        raise HTTPException(
            status_code=409,
            detail=f"Device '{device_id}' is already recording (session: {device.current_session_id})"
        )

    # Create a recording session
    db_session = _create_satellite_session(db, device, source_type="satellite_stream")

    device.status = "recording"
    device.current_session_id = db_session.session_id
    db.commit()

    logger.info(f"Started recording on satellite {device_id}, session {db_session.session_id}")

    return {
        "device_id": device_id,
        "session_id": db_session.session_id,
        "status": "recording",
        "message": "Recording started"
    }


@router.post("/{device_id}/stop-recording")
async def stop_satellite_recording(
    device_id: str,
    db: Session = Depends(get_db),
    presented_secret: Optional[str] = Depends(device_auth_from_http),
    active_org: Optional[ActiveOrganization] = Depends(_optional_active_org),
):
    """Signal a satellite device to stop recording.

    Dual-auth: device flow (Bearer/X-Device-Secret) OR user flow.
    """
    device, org_id = _resolve_dual_auth(
        db=db,
        device_id=device_id,
        presented_secret=presented_secret,
        active_org=active_org,
    )

    if device.status != "recording":
        raise HTTPException(
            status_code=409,
            detail=f"Device '{device_id}' is not currently recording (status: {device.status})"
        )

    # Update the recording session — scoped to caller's org so a forged
    # current_session_id (e.g., from a stale DB write) cannot mutate another
    # tenant's session row.
    if device.current_session_id:
        db_session = db.query(DBRecordingSession).filter(
            DBRecordingSession.session_id == device.current_session_id,
            DBRecordingSession.organization_id == org_id,
        ).first()
        if db_session:
            db_session.status = "processing"
            db_session.ended_at = datetime.now(timezone.utc)
            if db_session.started_at:
                started = db_session.started_at
                ended = db_session.ended_at
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
                db_session.duration = (ended - started).total_seconds()

    session_id = device.current_session_id
    device.status = "online"
    device.current_session_id = None
    db.commit()

    logger.info(f"Stopped recording on satellite {device_id}, session {session_id}")

    return {
        "device_id": device_id,
        "session_id": session_id,
        "status": "online",
        "message": "Recording stopped"
    }
