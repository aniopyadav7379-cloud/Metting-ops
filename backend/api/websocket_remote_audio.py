"""
WebSocket endpoint for remote companion-app audio streaming.

Accepts binary PCM frames (16-bit, 16kHz, mono) from companion apps
(Mac, PC, phone) that capture mic + system audio.  The companion app
creates a session via the REST API first, then connects here to stream
audio.  The server writes a progressive WAV file, feeds 15-second
chunks to Whisper for live transcription, and sends JSON transcription
results back to the client.

Authentication: JWT token passed as ``?token=...`` query parameter.

Control messages (JSON text frames from client):
  - {"action": "stop"}   - graceful stop
  - {"action": "ping"}   - keepalive (server responds with {"action": "pong"})
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from datetime import datetime, timezone
from typing import Dict, Optional
import asyncio
import json
import logging
import os
import struct
import tempfile
import time

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Audio parameters (must match companion app config)
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
BITS_PER_SAMPLE = 16
NUM_CHANNELS = 1
BYTES_PER_SAMPLE = BITS_PER_SAMPLE // 8

# How often to feed accumulated audio to the transcription pipeline (seconds)
TRANSCRIPTION_INTERVAL = 15
# Bytes of PCM data that correspond to TRANSCRIPTION_INTERVAL seconds
TRANSCRIPTION_CHUNK_BYTES = TRANSCRIPTION_INTERVAL * SAMPLE_RATE * BYTES_PER_SAMPLE

# Reconnection window (seconds) - if a client reconnects within this window,
# resume appending to the existing WAV file instead of starting a new one.
RECONNECTION_WINDOW = 60

# Recordings directory (single source of truth from working_audio_service)
try:
    from services.working_audio_service import WorkingAudioService
    RECORDINGS_DIR = WorkingAudioService.RECORDINGS_DIR
except Exception:
    RECORDINGS_DIR = os.environ.get(
        "RECORDINGS_DIR",
        # default beside this backend, not an absolute path from one machine
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "recordings"),
    )

# Track active remote-audio streams for reconnection support.
# Keyed by session_id (companion apps identify by session, not device).
_active_streams: Dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Progressive WAV writer
# ---------------------------------------------------------------------------
class ProgressiveWAVWriter:
    """Writes raw PCM data to a WAV file progressively.

    The WAV header is written with placeholder sizes on open; ``close()``
    seeks back and patches the header with the actual data size so the
    resulting file is a valid WAV.
    """

    def __init__(
        self,
        filepath: str,
        sample_rate: int = SAMPLE_RATE,
        channels: int = NUM_CHANNELS,
        sample_width: int = BYTES_PER_SAMPLE,
    ):
        self.filepath = filepath
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        self.data_size = 0
        self._closed = False
        self.file = open(filepath, "wb")
        self._write_header()

    def _write_header(self):
        """Write a WAV header with placeholder sizes (will be patched on close)."""
        self.file.seek(0)
        # RIFF header
        self.file.write(b"RIFF")
        self.file.write(struct.pack("<I", 0))  # placeholder file size
        self.file.write(b"WAVE")
        # fmt sub-chunk
        self.file.write(b"fmt ")
        self.file.write(struct.pack("<I", 16))  # sub-chunk size (PCM)
        self.file.write(struct.pack("<H", 1))  # PCM format
        self.file.write(struct.pack("<H", self.channels))
        self.file.write(struct.pack("<I", self.sample_rate))
        byte_rate = self.sample_rate * self.channels * self.sample_width
        self.file.write(struct.pack("<I", byte_rate))
        block_align = self.channels * self.sample_width
        self.file.write(struct.pack("<H", block_align))
        self.file.write(struct.pack("<H", self.sample_width * 8))
        # data sub-chunk
        self.file.write(b"data")
        self.file.write(struct.pack("<I", 0))  # placeholder data size

    def write(self, pcm_data: bytes):
        """Append raw PCM data to the file."""
        if self._closed:
            return
        self.file.write(pcm_data)
        self.data_size += len(pcm_data)

    def flush(self):
        """Flush the underlying file buffer."""
        if not self._closed:
            self.file.flush()

    def close(self):
        """Patch the WAV header with actual sizes and close the file."""
        if self._closed:
            return
        self._closed = True
        try:
            file_size = 36 + self.data_size
            self.file.seek(4)
            self.file.write(struct.pack("<I", file_size))
            self.file.seek(40)
            self.file.write(struct.pack("<I", self.data_size))
        except Exception as e:
            logger.error(f"Failed to finalize WAV header for {self.filepath}: {e}")
        finally:
            self.file.close()

    @property
    def duration_seconds(self) -> float:
        return self.data_size / (self.sample_rate * self.channels * self.sample_width)


# ---------------------------------------------------------------------------
# JWT authentication helper
# ---------------------------------------------------------------------------
async def _authenticate_websocket(websocket: WebSocket):
    """Validate a JWT token from the ``token`` query parameter.

    Returns the active ``User`` (detached from its session) on success, or
    *None* on failure. We hand back the full row — not just the id — so the
    WS handler can tier-gate (``user.tier`` / ``user.is_superuser``) and
    org-scope the session lookup without re-querying.

    We open + close our own ``SessionLocal()`` because Starlette WS handlers
    can't use ``Depends(get_db)``. The user's org-membership ids are
    materialised onto ``_org_ids`` before the session closes so org scoping
    survives detachment (mirrors streaming.py's ``_resolve_ws_user``).
    """
    token = websocket.query_params.get("token")
    if not token:
        return None

    try:
        from auth.utils import decode_token
        from auth.service import AuthService
        from auth.models import UserOrganization
        from database.database import SessionLocal

        payload = decode_token(token)
        if not payload or payload.get("type") != "access":
            return None

        user_id_str = payload.get("sub")
        if not user_id_str:
            return None

        user_id = int(user_id_str)

        # Verify user exists and is active
        db = SessionLocal()
        try:
            user = AuthService.get_user_by_id(db, user_id)
            if user and user.is_active:
                # Materialise org-membership ids while the session is open so
                # the org-scope check works after the session closes.
                org_ids = [
                    row[0]
                    for row in db.query(UserOrganization.organization_id)
                    .filter(UserOrganization.user_id == user.id)
                    .all()
                ]
                db.expunge(user)
                user._org_ids = org_ids
                return user
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"Remote-audio JWT auth failed: {e}")

    return None


# ---------------------------------------------------------------------------
# WAV header writer (standalone, for temp transcription chunks)
# ---------------------------------------------------------------------------
def _write_wav_header(f, num_samples: int = 0):
    """Write a minimal WAV header for *num_samples* of 16-bit mono 16 kHz audio."""
    data_size = num_samples * NUM_CHANNELS * BYTES_PER_SAMPLE
    file_size = 36 + data_size
    f.write(b"RIFF")
    f.write(struct.pack("<I", file_size))
    f.write(b"WAVE")
    f.write(b"fmt ")
    f.write(struct.pack("<I", 16))
    f.write(struct.pack("<H", 1))
    f.write(struct.pack("<H", NUM_CHANNELS))
    f.write(struct.pack("<I", SAMPLE_RATE))
    f.write(struct.pack("<I", SAMPLE_RATE * NUM_CHANNELS * BYTES_PER_SAMPLE))
    f.write(struct.pack("<H", NUM_CHANNELS * BYTES_PER_SAMPLE))
    f.write(struct.pack("<H", BITS_PER_SAMPLE))
    f.write(b"data")
    f.write(struct.pack("<I", data_size))


# ---------------------------------------------------------------------------
# Main WebSocket endpoint
# ---------------------------------------------------------------------------
@router.websocket("/ws/remote-audio/{session_id}")
async def websocket_remote_audio(websocket: WebSocket, session_id: str):
    """Accept a binary PCM audio stream from a companion app.

    The companion app must:
    1. Create a session via ``POST /api/simple/recording-sessions``.
    2. Connect here with ``?token=<jwt>``.
    3. Send binary WebSocket frames containing raw 16-bit 16 kHz mono PCM.
    4. Optionally send JSON text frames: ``{"action": "stop"}`` or ``{"action": "ping"}``.
    5. Disconnect when done (or send ``stop``).
    """
    # --- Authenticate ---
    user = await _authenticate_websocket(websocket)
    if user is None:
        # Reject the connection before accepting
        await websocket.close(code=4001)
        logger.warning(f"Remote-audio: rejected unauthenticated connection for session {session_id}")
        return

    user_id = user.id

    # --- Tier gate (before accepting any audio frames) ---
    # Appending remote audio drives the canonical server reprocess path, which
    # is a paid-tier capability. Free-tier users are rejected with 4403 before
    # the handshake completes. Internal-service principals bypass (none reach
    # this JWT path today, but gate_feature_for_caller stays consistent with
    # the HTTP gates). Mirrors streaming.py's tier-rejection shape (close-code
    # in the application range), using 4403 to signal "authenticated but
    # forbidden" per this module's existing 44xx convention.
    from auth.tier import gate_feature_for_caller
    from auth.ws_auth import resolve_session_org

    # billing-1: the canonical reprocess this stream drives must be covered by
    # the ACTIVE workspace's plan (the org that owns the session), not just the
    # user's global tier. Resolve the session's org and pass it to the gate;
    # when unresolved, gate_feature_for_caller falls back to the user tier.
    active_org = resolve_session_org(session_id)

    try:
        gate_feature_for_caller(user, "canonical_reprocess", active_org)
    except HTTPException:
        logger.warning(
            f"Remote-audio: tier_insufficient for user_id={user_id} "
            f"session {session_id} - rejecting with 4403"
        )
        await websocket.close(code=4403)
        return

    await websocket.accept()
    logger.info(f"Remote-audio: connected for session {session_id} (user_id={user_id})")

    # --- Database setup ---
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession as DBRecordingSession

        db = SessionLocal()
    except Exception as e:
        logger.error(f"Remote-audio: database connection failed: {e}")
        await websocket.send_json({"type": "error", "message": "Server database unavailable"})
        await websocket.close(code=1011)
        return

    wav_writer: Optional[ProgressiveWAVWriter] = None
    file_path: Optional[str] = None
    db_session_id: Optional[int] = None
    total_bytes = 0
    resuming = False

    try:
        # --- Check for reconnection to an existing stream ---
        existing = _active_streams.get(session_id)
        if existing:
            elapsed = time.time() - existing.get("disconnect_time", 0)
            if elapsed < RECONNECTION_WINDOW and existing.get("file_path"):
                resuming = True
                file_path = existing["file_path"]
                db_session_id = existing["db_session_id"]
                total_bytes = existing.get("total_bytes", 0)
                logger.info(
                    f"Remote-audio: resuming session {session_id} "
                    f"(reconnected in {elapsed:.0f}s, {total_bytes} bytes so far)"
                )

        # --- Validate session exists AND belongs to the caller's org ---
        # Mirrors the org-scoping the HTTP session endpoints use
        # (sessions.py: RecordingSession.organization_id == active_org.id).
        # Without this, any user with a valid JWT could append audio to any
        # session UUID across org boundaries. Superusers bypass the org
        # filter (support/admin access), matching resolve_active_organization.
        org_ids = getattr(user, "_org_ids", []) or []
        session_query = db.query(DBRecordingSession).filter(
            DBRecordingSession.session_id == session_id
        )
        if not user.is_superuser:
            # in_([]) is always-false on every backend, so an orgless user
            # matches nothing — exactly the desired deny.
            session_query = session_query.filter(
                DBRecordingSession.organization_id.in_(org_ids)
            )
        session = session_query.first()

        if not session:
            # Collapse "doesn't exist" and "exists in another org" into one
            # 4403 so we never leak cross-org session existence. (A truly
            # missing session for an in-org caller is also forbidden access.)
            logger.warning(
                f"Remote-audio: session {session_id} not found or cross-org "
                f"for user_id={user_id} (org_ids={org_ids}) - rejecting 4403"
            )
            await websocket.send_json({"type": "error", "message": "Session not accessible"})
            await websocket.close(code=4403)
            db.close()
            return

        db_session_id = session.id

        # billing-1: the pre-accept gate above only checked the caller's global
        # tier. Paid server compute must ALSO require the SESSION'S workspace to
        # be on a covering plan — else a user who is Pro in another org could
        # stream paid compute into a FREE org's session. Superusers bypass.
        if not user.is_superuser:
            from auth.organization import load_organization
            from auth.tier import org_covers_feature

            sess_org = load_organization(db, session.organization_id)
            if not org_covers_feature(sess_org, "canonical_reprocess"):
                logger.warning(
                    f"Remote-audio: workspace plan lacks canonical_reprocess for "
                    f"session {session_id} (org_id={session.organization_id}) - "
                    f"rejecting 4403"
                )
                await websocket.send_json(
                    {"type": "error", "message": "Workspace plan does not include server processing"}
                )
                await websocket.close(code=4403)
                db.close()
                return

        if resuming and file_path:
            # Re-open the WAV file in append mode via raw file handle
            # We can't use ProgressiveWAVWriter here because the header
            # is already written; just open for append.
            wav_writer = None  # handled below
        else:
            # --- Create WAV file ---
            os.makedirs(RECORDINGS_DIR, exist_ok=True)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"companion_{session_id[:8]}_{timestamp}.wav"
            file_path = os.path.normpath(os.path.join(RECORDINGS_DIR, filename))

            wav_writer = ProgressiveWAVWriter(file_path)
            total_bytes = 0

            logger.info(f"Remote-audio: created WAV file {file_path}")

        # --- Update session in DB ---
        session.status = "recording"
        session.source_type = "companion_app"
        session.audio_file = os.path.normpath(file_path)
        if not session.started_at:
            session.started_at = datetime.now(timezone.utc)
        db.commit()

        # Track this stream for reconnection
        _active_streams[session_id] = {
            "file_path": file_path,
            "db_session_id": db_session_id,
            "total_bytes": total_bytes,
            "disconnect_time": 0,
        }

        # Send confirmation to client
        await websocket.send_json({
            "type": "status",
            "status": "recording",
            "session_id": session_id,
            "resumed": resuming,
        })

    except Exception as e:
        logger.error(f"Remote-audio: setup error for session {session_id}: {e}")
        await websocket.send_json({"type": "error", "message": f"Setup failed: {str(e)}"})
        await websocket.close(code=1011)
        db.close()
        return
    finally:
        db.close()

    # --- If resuming, open file in append mode ---
    raw_append_file = None
    if resuming and file_path and wav_writer is None:
        try:
            raw_append_file = open(file_path, "ab")
        except Exception as e:
            logger.error(f"Remote-audio: failed to reopen WAV for append: {e}")
            await websocket.send_json({"type": "error", "message": "Failed to resume recording"})
            await websocket.close(code=1011)
            return

    # --- Main streaming loop ---
    last_transcription_time = time.time()
    chunk_buffer = bytearray()
    stopped = False

    try:
        while True:
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=60.0)
            except asyncio.TimeoutError:
                # Send keepalive; if client doesn't respond, the next receive
                # will raise WebSocketDisconnect.
                try:
                    await websocket.send_json({"type": "keepalive"})
                except Exception:
                    break
                continue

            # --- Binary frame: raw PCM audio data ---
            if "bytes" in message and message["bytes"]:
                data = message["bytes"]
                if wav_writer:
                    wav_writer.write(data)
                elif raw_append_file:
                    raw_append_file.write(data)
                total_bytes += len(data)
                chunk_buffer.extend(data)

                # Update tracking
                _active_streams.get(session_id, {})["total_bytes"] = total_bytes

                # Periodic transcription
                now = time.time()
                if (now - last_transcription_time >= TRANSCRIPTION_INTERVAL
                        and len(chunk_buffer) >= TRANSCRIPTION_CHUNK_BYTES):
                    last_transcription_time = now

                    if wav_writer:
                        wav_writer.flush()
                    elif raw_append_file:
                        raw_append_file.flush()

                    pcm_chunk = bytes(chunk_buffer)
                    chunk_buffer.clear()

                    chunk_offset = (total_bytes - len(pcm_chunk)) / (
                        SAMPLE_RATE * BYTES_PER_SAMPLE
                    )

                    asyncio.create_task(
                        _transcribe_and_send(
                            websocket=websocket,
                            session_id=session_id,
                            db_session_id=db_session_id,
                            pcm_data=pcm_chunk,
                            chunk_offset_seconds=chunk_offset,
                        )
                    )

            # --- Text frame: control messages ---
            elif "text" in message and message["text"]:
                try:
                    msg = json.loads(message["text"])
                except (json.JSONDecodeError, TypeError):
                    continue

                action = msg.get("action", "")
                if action == "stop":
                    logger.info(f"Remote-audio: client sent stop for session {session_id}")
                    stopped = True
                    break
                elif action == "ping":
                    await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info(f"Remote-audio: client disconnected from session {session_id}")
    except Exception as e:
        logger.error(f"Remote-audio: streaming error for session {session_id}: {e}")
    finally:
        # --- Close the WAV file ---
        if wav_writer:
            wav_writer.close()
            logger.info(
                f"Remote-audio: finalized WAV {file_path} "
                f"({total_bytes} bytes, {wav_writer.duration_seconds:.1f}s)"
            )
        elif raw_append_file:
            raw_append_file.close()
            # Patch the WAV header with the updated total size
            _finalize_wav_header(file_path, total_bytes)

        # Record disconnect time for reconnection window
        _active_streams[session_id] = {
            "file_path": file_path,
            "db_session_id": db_session_id,
            "total_bytes": total_bytes,
            "disconnect_time": time.time(),
        }

        if stopped:
            # Immediate finalization when client explicitly stops
            await _finalize_session(session_id, db_session_id, file_path)
            _active_streams.pop(session_id, None)
        else:
            # Schedule deferred finalization (reconnection window)
            asyncio.create_task(
                _finalize_if_no_reconnect(session_id, db_session_id, file_path)
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finalize_wav_header(file_path: str, total_data_bytes: int):
    """Patch the WAV header in an existing file with actual sizes."""
    try:
        with open(file_path, "r+b") as f:
            file_size = 36 + total_data_bytes
            f.seek(4)
            f.write(struct.pack("<I", file_size))
            f.seek(40)
            f.write(struct.pack("<I", total_data_bytes))
        logger.info(f"Remote-audio: patched WAV header for {file_path} ({total_data_bytes} bytes)")
    except Exception as e:
        logger.error(f"Remote-audio: failed to patch WAV header for {file_path}: {e}")


async def _finalize_session(
    session_id: str,
    db_session_id: int,
    file_path: str,
):
    """Update the DB session to 'processing' and trigger process_recording()."""
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession as DBRecordingSession

        db = SessionLocal()
        try:
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
                session.audio_file = os.path.normpath(file_path)
                db.commit()

                # Trigger full post-processing pipeline
                from api.simple_recording_db import process_recording
                asyncio.create_task(
                    process_recording(
                        session_id=session.session_id,
                        audio_file=file_path,
                        db_session_id=session.id,
                    )
                )
                logger.info(f"Remote-audio: triggered post-processing for session {session_id}")
            else:
                logger.error(f"Remote-audio: session {session_id} (db id {db_session_id}) not found for finalization")
        except Exception as e:
            logger.error(f"Remote-audio: finalization DB error for session {session_id}: {e}")
            db.rollback()
        finally:
            db.close()
    except Exception as e:
        logger.error(f"Remote-audio: finalization error for session {session_id}: {e}")


async def _finalize_if_no_reconnect(
    session_id: str,
    db_session_id: int,
    file_path: str,
):
    """Wait for the reconnection window, then finalize if the client did not reconnect."""
    await asyncio.sleep(RECONNECTION_WINDOW + 5)

    stream_info = _active_streams.get(session_id)
    if not stream_info:
        return  # already cleaned up (client reconnected and stopped normally)

    # If disconnect_time is still set, no reconnection happened
    if stream_info.get("disconnect_time", 0) > 0:
        _active_streams.pop(session_id, None)
        logger.info(
            f"Remote-audio: no reconnection for session {session_id}, finalizing"
        )
        await _finalize_session(session_id, db_session_id, file_path)


async def _transcribe_and_send(
    websocket: WebSocket,
    session_id: str,
    db_session_id: int,
    pcm_data: bytes,
    chunk_offset_seconds: float,
):
    """Transcribe a PCM chunk and send the result back to the WebSocket client.

    Also stores the transcription segments in the database so they are
    available even if the client missed the WebSocket message.
    """
    if len(pcm_data) < SAMPLE_RATE * BYTES_PER_SAMPLE:
        # Less than 1 second of audio - skip
        return

    try:
        # Write PCM chunk to a temporary WAV file for Whisper
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
            _write_wav_header(tmp, num_samples=len(pcm_data) // BYTES_PER_SAMPLE)
            tmp.write(pcm_data)

        try:
            # Try whisper_server_client first (Vulkan iGPU), then real_whisper_service
            result = None

            try:
                from services.whisper_server_client import whisper_server_client
                if whisper_server_client.is_available():
                    result = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None,
                            whisper_server_client.transcribe_file,
                            tmp_path,
                        ),
                        timeout=30,
                    )
            except Exception:
                pass

            if not result:
                try:
                    from services.real_whisper_service import real_whisper_service
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            real_whisper_service.transcribe_file,
                            tmp_path,
                            False,  # diarize=False for live chunks
                        ),
                        timeout=30,
                    )
                except Exception:
                    pass

            if result and result.get("segments"):
                segments_out = []
                from database.database import SessionLocal
                from database.models import Transcription

                db = SessionLocal()
                try:
                    for seg in result["segments"]:
                        text = seg.get("text", "").strip()
                        if not text:
                            continue

                        start_time = chunk_offset_seconds + float(seg.get("start", 0))
                        end_time = chunk_offset_seconds + float(seg.get("end", 0))
                        confidence = float(seg.get("confidence", 0.9))

                        # Store in DB
                        trans = Transcription(
                            session_id=db_session_id,
                            text=text,
                            speaker=seg.get("speaker"),
                            start_time=start_time,
                            end_time=end_time,
                            confidence=confidence,
                        )
                        db.add(trans)

                        segments_out.append({
                            "text": text,
                            "speaker": seg.get("speaker"),
                            "start": start_time,
                            "end": end_time,
                            "confidence": confidence,
                        })

                    db.commit()

                    logger.debug(
                        f"Remote-audio: transcribed {len(segments_out)} segments "
                        f"at offset {chunk_offset_seconds:.1f}s for session {session_id}"
                    )
                except Exception as e:
                    logger.error(f"Remote-audio: failed to save transcription segments: {e}")
                    db.rollback()
                finally:
                    db.close()

                # Send result back to client
                if segments_out:
                    try:
                        await websocket.send_json({
                            "type": "transcription",
                            "segments": segments_out,
                            "offset": chunk_offset_seconds,
                        })
                    except Exception:
                        # Client may have disconnected; non-fatal
                        pass

            elif result and result.get("text"):
                text = result["text"].strip()
                if text:
                    start_t = chunk_offset_seconds
                    end_t = chunk_offset_seconds + len(pcm_data) / (SAMPLE_RATE * BYTES_PER_SAMPLE)

                    from database.database import SessionLocal
                    from database.models import Transcription

                    db = SessionLocal()
                    try:
                        trans = Transcription(
                            session_id=db_session_id,
                            text=text,
                            speaker=None,
                            start_time=start_t,
                            end_time=end_t,
                            confidence=float(result.get("confidence", 0.9)),
                        )
                        db.add(trans)
                        db.commit()
                    except Exception as e:
                        logger.error(f"Remote-audio: failed to save text transcription: {e}")
                        db.rollback()
                    finally:
                        db.close()

                    try:
                        await websocket.send_json({
                            "type": "transcription",
                            "segments": [{
                                "text": text,
                                "speaker": None,
                                "start": start_t,
                                "end": end_t,
                                "confidence": float(result.get("confidence", 0.9)),
                            }],
                            "offset": chunk_offset_seconds,
                        })
                    except Exception:
                        pass

        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    except asyncio.TimeoutError:
        logger.warning(f"Remote-audio: transcription timed out for session {session_id}")
    except Exception as e:
        logger.error(f"Remote-audio: transcription error for session {session_id}: {e}")
