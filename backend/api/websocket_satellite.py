"""
WebSocket endpoint for satellite device audio streaming.

Accepts binary PCM frames (16-bit, 16kHz, mono) from satellite devices,
writes them to a WAV file progressively, and triggers transcription
every 15 seconds. On disconnect, finalizes the WAV and runs the full
post-processing pipeline (transcription + AI summary).

Authentication (task #85):
  * Every connection MUST present the device's per-device secret, issued
    at pairing-code redemption time.
  * Either ``Authorization: Bearer <secret>`` header or ``?token=<secret>``
    query param is accepted — browser WebSocket clients can't set
    custom headers, hence the query-param fallback.
  * Mismatch / missing / unknown-device ⇒ accept-then-close with code
    1008 (policy violation). Five failures inside any 10-minute window
    per device_id triggers a 30-minute lockout (in-memory limiter).
  * A device with NULL ``device_secret`` (legacy / orphan) cannot
    authenticate — it must re-pair.
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
import asyncio
import json
import logging
import os
import struct
import time
import uuid

# WebSocket close codes used by this module. 1008 = policy violation
# (RFC 6455 §7.4.1) — the correct code for authentication failures.
_WS_CLOSE_POLICY_VIOLATION = 1008

router = APIRouter()
logger = logging.getLogger(__name__)

# Audio parameters (must match satellite firmware)
SAMPLE_RATE = 16000
BITS_PER_SAMPLE = 16
NUM_CHANNELS = 1
BYTES_PER_SAMPLE = BITS_PER_SAMPLE // 8

# How often to feed accumulated audio to the transcription pipeline (seconds)
TRANSCRIPTION_INTERVAL = 15

# Reconnection window: if a device reconnects within this many seconds, resume session
RECONNECTION_WINDOW = 60

# Recordings directory
RECORDINGS_DIR = os.environ.get(
    "RECORDINGS_DIR",
    # default beside this backend, not an absolute path from one machine
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings"),
)

# Track active satellite streaming sessions for reconnection
_active_streams: Dict[str, dict] = {}


def _write_wav_header(f, num_samples: int = 0):
    """Write a WAV file header. If num_samples=0, writes a placeholder
    that must be updated when the file is finalized."""
    data_size = num_samples * NUM_CHANNELS * BYTES_PER_SAMPLE
    file_size = 36 + data_size  # 36 = header size minus 8 bytes for RIFF chunk

    f.write(b"RIFF")
    f.write(struct.pack("<I", file_size))
    f.write(b"WAVE")

    # fmt sub-chunk
    f.write(b"fmt ")
    f.write(struct.pack("<I", 16))  # Sub-chunk size
    f.write(struct.pack("<H", 1))   # PCM format
    f.write(struct.pack("<H", NUM_CHANNELS))
    f.write(struct.pack("<I", SAMPLE_RATE))
    f.write(struct.pack("<I", SAMPLE_RATE * NUM_CHANNELS * BYTES_PER_SAMPLE))  # Byte rate
    f.write(struct.pack("<H", NUM_CHANNELS * BYTES_PER_SAMPLE))  # Block align
    f.write(struct.pack("<H", BITS_PER_SAMPLE))

    # data sub-chunk
    f.write(b"data")
    f.write(struct.pack("<I", data_size))


def _finalize_wav(file_path: str, total_bytes_written: int):
    """Update the WAV header with the actual data size."""
    try:
        with open(file_path, "r+b") as f:
            file_size = 36 + total_bytes_written
            f.seek(4)
            f.write(struct.pack("<I", file_size))
            f.seek(40)
            f.write(struct.pack("<I", total_bytes_written))
        logger.info(f"Finalized WAV header: {file_path} ({total_bytes_written} bytes of audio)")
    except Exception as e:
        logger.error(f"Failed to finalize WAV header for {file_path}: {e}")


def _extract_ws_device_secret(websocket: WebSocket) -> Optional[str]:
    """Pull the device secret out of a WebSocket request.

    Accepts:
      * ``Authorization: Bearer <secret>`` header (preferred when the
        client can set headers — ESP-IDF, Python clients)
      * ``?token=<secret>`` query parameter (fallback for browsers, which
        cannot set headers on the WebSocket handshake)

    Header takes precedence over query param. Returns None if neither is
    present. The WebSocket endpoint is device-only — there is no
    admin/user JWT path here — but we keep the extraction logic
    consistent with the HTTP path for clarity.
    """
    auth_header = websocket.headers.get("authorization") or websocket.headers.get(
        "Authorization"
    )
    if auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            candidate = parts[1].strip()
            if candidate:
                return candidate

    token = websocket.query_params.get("token")
    if token:
        token = token.strip()
        if token:
            return token

    return None


def _ensure_organization_id_set(db_session, device) -> None:
    """Pin the recording session to the device's org_id.

    The org column is NOT NULL — without this assignment SQLite + Postgres
    will both refuse the insert. Existing code paths already set this when
    creating via /api/satellites/upload-audio (see _create_satellite_session
    in satellite_api.py); the WS path was missing it because the original
    handshake predates multi-org scoping.
    """
    if getattr(db_session, "organization_id", None) is None:
        db_session.organization_id = device.organization_id


@router.websocket("/ws/satellite/{device_id}/audio")
async def websocket_satellite_audio(websocket: WebSocket, device_id: str):
    """WebSocket endpoint for real-time satellite audio streaming.

    Accepts binary frames of raw 16-bit PCM at 16kHz mono. Authentication
    happens immediately after ``accept()`` — invalid credentials close
    the connection with 1008 (policy violation) before any frames are
    consumed.
    """
    # Accept first so we can send a structured close frame on failure.
    # Per RFC 6455 §7.4.1, code 1008 is the correct close code for an
    # authentication failure; the body is intentionally generic so an
    # attacker can't distinguish "unknown device" from "wrong secret"
    # from "locked out".
    await websocket.accept()

    presented_secret = _extract_ws_device_secret(websocket)

    # --- Authenticate (rate-limited, generic failure) ---
    try:
        from auth.device_auth import (
            DeviceAuthError,
            authenticate_device,
        )
        from database.database import SessionLocal
        from database.models import RecordingSession as DBRecordingSession

        db = SessionLocal()
    except Exception as e:
        logger.error(f"Satellite WS: failed to import auth/db modules: {e}")
        await websocket.close(code=1011)
        return

    try:
        try:
            auth_result = authenticate_device(
                db=db,
                device_id=device_id,
                plaintext_secret=presented_secret,
            )
        except DeviceAuthError:
            # Never log the presented secret. Never include it in any
            # outgoing frame. The presence/absence of the device row is
            # an authentication-internal detail.
            try:
                await websocket.send_json({"error": "auth_failed"})
            except Exception:
                pass
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
            return
    except Exception as e:
        logger.error(f"Satellite WS auth path crashed for device_id=%s: %s", device_id, e)
        try:
            await websocket.send_json({"error": "auth_error"})
        except Exception:
            pass
        await websocket.close(code=1011)
        return
    finally:
        # We pulled the row above; the rest of the handshake re-queries
        # by id and uses its own session. Close the auth session early
        # to free the connection.
        pass

    device = auth_result.device
    logger.info(
        "Satellite WebSocket authenticated: device_id=%s (org=%s)",
        device_id,
        device.organization_id,
    )

    # --- Check for reconnection to an existing session ---
    existing_stream = _active_streams.get(device_id)
    resuming = False
    if existing_stream:
        elapsed = time.time() - existing_stream.get("disconnect_time", 0)
        if elapsed < RECONNECTION_WINDOW and existing_stream.get("file_path"):
            resuming = True
            logger.info(f"Satellite {device_id} reconnected within {elapsed:.0f}s, resuming session")

    # --- Set up recording on the auth-session ---
    try:
        # Re-fetch the device on this DB session so we have a fresh
        # attached instance for the recording flow.
        from database.models import SatelliteDevice as _SatDev
        device = db.query(_SatDev).filter(_SatDev.device_id == device_id).first()

        if not device:
            # Should be impossible — we just authenticated. Treat as a
            # race (device deleted between auth and recording setup).
            logger.warning(f"Satellite device disappeared post-auth: {device_id}")
            try:
                await websocket.send_json({"error": "device_gone"})
            except Exception:
                pass
            await websocket.close(code=_WS_CLOSE_POLICY_VIOLATION)
            db.close()
            return

        if resuming and existing_stream:
            # Resume existing session
            file_path = existing_stream["file_path"]
            session_id = existing_stream["session_id"]
            db_session_id = existing_stream["db_session_id"]
            total_bytes = existing_stream.get("total_bytes", 0)
            wav_file = open(file_path, "ab")  # Append mode
            logger.info(f"Resumed WAV file: {file_path} (at {total_bytes} bytes)")
        else:
            # Create new session
            session_id = str(uuid.uuid4())
            os.makedirs(RECORDINGS_DIR, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"satellite_{device_id}_{timestamp}.wav"
            file_path = os.path.join(RECORDINGS_DIR, filename)

            # Create WAV file with placeholder header
            wav_file = open(file_path, "wb")
            _write_wav_header(wav_file, num_samples=0)
            total_bytes = 0

            # Create recording session in database. Pin org_id to the
            # device's org — this is a NOT NULL column for multi-org
            # scoping. Matches the same convention used by the satellite
            # HTTP upload path (_create_satellite_session in
            # satellite_api.py).
            session_name = f"{device.name or device_id} - {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
            db_session = DBRecordingSession(
                session_id=session_id,
                name=session_name,
                title=session_name,
                description=f"Live stream from satellite: {device_id}",
                status="recording",
                created_at=datetime.now(timezone.utc),
                started_at=datetime.now(timezone.utc),
                duration=0.0,
                source_device_id=device_id,
                source_type="satellite_stream",
                room_name=device.room_name,
                audio_file=file_path,
                organization_id=device.organization_id,
            )
            db.add(db_session)
            db.commit()
            db.refresh(db_session)
            db_session_id = db_session.id

            logger.info(f"Created satellite stream session: {session_id} -> {file_path}")

        # Update device status
        device.status = "recording"
        device.current_session_id = session_id
        device.last_heartbeat = datetime.now(timezone.utc)
        db.commit()

        # Track this stream for reconnection
        _active_streams[device_id] = {
            "file_path": file_path,
            "session_id": session_id,
            "db_session_id": db_session_id,
            "total_bytes": total_bytes,
            "disconnect_time": 0,
        }

        # Send session info back to the device
        await websocket.send_json({
            "session_id": session_id,
            "status": "recording",
            "resumed": resuming,
        })

    except Exception as e:
        logger.error(f"Error setting up satellite stream for {device_id}: {e}")
        await websocket.send_json({"error": f"Setup failed: {str(e)}"})
        await websocket.close(code=1011)
        db.close()
        return
    finally:
        db.close()

    # --- Main streaming loop ---
    last_transcription_time = time.time()
    chunk_buffer = bytearray()  # Buffer for transcription chunks

    try:
        while True:
            data = await websocket.receive_bytes()

            if not data:
                continue

            # Write PCM data to WAV file
            wav_file.write(data)
            total_bytes += len(data)
            chunk_buffer.extend(data)

            # Update stream tracking
            _active_streams[device_id]["total_bytes"] = total_bytes

            # Periodic transcription every TRANSCRIPTION_INTERVAL seconds
            now = time.time()
            if now - last_transcription_time >= TRANSCRIPTION_INTERVAL and len(chunk_buffer) > 0:
                last_transcription_time = now

                # Flush WAV file to ensure data is written
                wav_file.flush()

                # Feed chunk to transcription in background (non-blocking)
                chunk_bytes = bytes(chunk_buffer)
                chunk_buffer.clear()

                asyncio.create_task(
                    _transcribe_chunk(
                        device_id=device_id,
                        session_id=session_id,
                        db_session_id=db_session_id,
                        pcm_data=chunk_bytes,
                        chunk_offset_seconds=(total_bytes - len(chunk_bytes)) / (SAMPLE_RATE * BYTES_PER_SAMPLE),
                    )
                )

    except WebSocketDisconnect:
        logger.info(f"Satellite {device_id} disconnected")
    except Exception as e:
        logger.error(f"Satellite stream error for {device_id}: {e}")
    finally:
        # Close WAV file and finalize header
        wav_file.close()
        _finalize_wav(file_path, total_bytes)

        # Record disconnect time for reconnection window
        _active_streams[device_id] = {
            "file_path": file_path,
            "session_id": session_id,
            "db_session_id": db_session_id,
            "total_bytes": total_bytes,
            "disconnect_time": time.time(),
        }

        # Schedule cleanup: if no reconnection within window, finalize the session
        asyncio.create_task(
            _finalize_if_no_reconnect(device_id, session_id, db_session_id, file_path)
        )


async def _finalize_if_no_reconnect(
    device_id: str,
    session_id: str,
    db_session_id: int,
    file_path: str,
):
    """Wait for the reconnection window, then finalize the session if the device
    didn't reconnect."""
    await asyncio.sleep(RECONNECTION_WINDOW + 5)

    stream_info = _active_streams.get(device_id)
    if not stream_info:
        return

    # If disconnect_time is still set (no reconnection happened), finalize
    if stream_info.get("disconnect_time", 0) > 0:
        _active_streams.pop(device_id, None)
        logger.info(f"Finalizing satellite session {session_id} (no reconnection from {device_id})")

        try:
            from database.database import SessionLocal
            from database.models import SatelliteDevice, RecordingSession as DBRecordingSession

            db = SessionLocal()
            try:
                # Update device status
                device = db.query(SatelliteDevice).filter(
                    SatelliteDevice.device_id == device_id
                ).first()
                if device:
                    device.status = "online"
                    device.current_session_id = None

                # Update session
                session = db.query(DBRecordingSession).filter(
                    DBRecordingSession.id == db_session_id
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

                db.commit()

                # Trigger full processing pipeline
                from api.simple_recording_db import process_recording
                asyncio.create_task(
                    process_recording(
                        session_id=session_id,
                        audio_file=file_path,
                        db_session_id=db_session_id,
                    )
                )
                logger.info(f"Triggered post-processing for satellite session {session_id}")
            except Exception as e:
                logger.error(f"Error finalizing satellite session: {e}")
                db.rollback()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Database error during satellite session finalization: {e}")


async def _transcribe_chunk(
    device_id: str,
    session_id: str,
    db_session_id: int,
    pcm_data: bytes,
    chunk_offset_seconds: float,
):
    """Transcribe a chunk of PCM audio and store the result.

    This runs in the background and does not block the streaming loop.
    """
    if len(pcm_data) < SAMPLE_RATE * BYTES_PER_SAMPLE:
        # Less than 1 second of audio, skip
        return

    try:
        import tempfile
        from services.real_whisper_service import real_whisper_service

        # Write chunk to a temporary WAV file for Whisper
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
            _write_wav_header(tmp, num_samples=len(pcm_data) // BYTES_PER_SAMPLE)
            tmp.write(pcm_data)

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    real_whisper_service.transcribe_file,
                    tmp_path,
                    diarize=False,
                ),
                timeout=30,
            )

            if result and "segments" in result:
                from database.database import SessionLocal
                from database.models import Transcription

                db = SessionLocal()
                try:
                    for seg in result["segments"]:
                        trans = Transcription(
                            session_id=db_session_id,
                            text=seg.get("text", ""),
                            speaker=seg.get("speaker"),
                            start_time=chunk_offset_seconds + float(seg.get("start", 0)),
                            end_time=chunk_offset_seconds + float(seg.get("end", 0)),
                            confidence=float(seg.get("confidence", 0.9)),
                        )
                        db.add(trans)
                    db.commit()
                    logger.debug(
                        f"Satellite {device_id}: transcribed {len(result['segments'])} segments "
                        f"at offset {chunk_offset_seconds:.1f}s"
                    )
                except Exception as e:
                    logger.error(f"Failed to save satellite transcription segments: {e}")
                    db.rollback()
                finally:
                    db.close()
        finally:
            # Clean up temp file
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    except asyncio.TimeoutError:
        logger.warning(f"Satellite chunk transcription timed out for {device_id}")
    except Exception as e:
        logger.error(f"Satellite chunk transcription error for {device_id}: {e}")
