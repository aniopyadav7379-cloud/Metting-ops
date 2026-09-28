"""Conference Room Recorder service.

Manages per-room `arecord` subprocesses on the backend host. One
``RoomRecorder`` per active room; multiple rooms run concurrently by design
(each binds to its own ``hw:X,0`` ALSA device). Each recorder runs a VAD
loop that emits one chunk per speech turn and POSTs it into the existing
``/api/recordings/sessions/{session_id}/chunks`` endpoint over the loopback
interface so the room path reuses 100% of the upload + transcription +
diarization pipeline that browser always-on sessions already exercise.

Why HTTP loopback instead of in-process calls:
  * The chunk endpoint does a lot of work — STT, diarization, transcript
    merge, WebSocket broadcast, audio file registration. Calling it via
    HTTP keeps the recorder loosely coupled and reuses the org-scoped auth
    pipeline.
  * Production deployment runs uvicorn with multiple workers; in-process
    state for "which worker owns which room" gets messy. Loopback HTTP
    sidesteps that — any worker can host the recorder.
  * The recorder doesn't need to know about diarization toggles, provider
    selection, etc. The endpoint resolves all of that per-org.

VAD:
  * ``webrtcvad`` is already pinned in requirements-docker*.txt
    (2.0.10). Lightweight, no extra deps, runs entirely on CPU.
  * Mode 2 is the WebRTC "moderate" sensitivity — good balance of false
    positives (background noise triggering recording) vs cutoffs (chopping
    a real word in half). 30 ms frames, 16 kHz mono, 16-bit linear PCM.
  * On VAD speech-end (after MIN_SILENCE_MS of consecutive non-speech
    frames), we flush the accumulated buffer to a WAV and POST it. The
    chunk is bounded at MAX_CHUNK_MS so a long monologue still gets
    segmented (and incremental transcripts can render in the UI).

Failure behaviour:
  * If `arecord` exits unexpectedly (USB unplugged, ALSA error), we log,
    flip the session/room to ``error``, and let the admin restart manually.
    No auto-restart in v1 — silent restart loops are worse than a visible
    failure state.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import struct
import subprocess
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import httpx

from database.database import SessionLocal
from database.models import RecordingSession
from database.models_rooms import ConferenceRoom, RoomAudioSource


logger = logging.getLogger(__name__)


# === Configuration ======================================================

# ALSA capture format. WebRTC VAD requires 8/16/32 kHz mono 16-bit PCM. We
# use 16 kHz because that matches Parakeet's preferred sample rate (avoids
# a downstream resample) and is the same rate the browser always-on path
# emits.
SAMPLE_RATE = 16_000
SAMPLE_WIDTH_BYTES = 2  # S16_LE
CHANNELS = 1

# WebRTC VAD frame size: 30 ms at 16 kHz = 480 samples = 960 bytes.
VAD_FRAME_MS = 30
VAD_FRAME_BYTES = int(SAMPLE_RATE * VAD_FRAME_MS / 1000) * SAMPLE_WIDTH_BYTES * CHANNELS

# A chunk closes when we see this many consecutive non-speech frames after
# at least one speech frame. 800 ms feels natural for sentence breaks
# without chopping mid-pause.
MIN_SILENCE_MS = 800
MAX_TRAILING_NON_SPEECH_FRAMES = MIN_SILENCE_MS // VAD_FRAME_MS

# Hard cap on a single chunk so a long monologue still flushes incrementally.
# 30 seconds keeps each transcription request bounded.
MAX_CHUNK_MS = 30_000
MAX_CHUNK_FRAMES = MAX_CHUNK_MS // VAD_FRAME_MS

# Minimum chunk before we bother POSTing. <300 ms of speech is almost
# always a cough / chair shuffle / VAD blip.
MIN_CHUNK_MS = 300
MIN_CHUNK_FRAMES = MIN_CHUNK_MS // VAD_FRAME_MS

# Internal loopback. The backend listens on 9050 by default. We post over
# HTTP (not HTTPS) on the loopback interface — the request never leaves
# the container.
INTERNAL_API_BASE = os.getenv("ROOM_RECORDER_API_BASE", "http://localhost:9050")
PROVENANCE_TAG = "room-server-usb-mic"

# Shared secret for backend-to-backend loopback auth. The chunks endpoints
# accept this token in an ``X-Internal-Service-Token`` header instead of a
# user JWT. Empty/unset == feature disabled, and chunk POSTs will 401 at
# the dual-auth dependency. We log loudly at recorder-start so a missing
# env var fails visibly instead of silently dropping audio.
INTERNAL_SERVICE_TOKEN_ENV = "INTERNAL_SERVICE_TOKEN"
INTERNAL_TOKEN_HEADER = "X-Internal-Service-Token"


def _read_internal_token() -> str:
    """Read INTERNAL_SERVICE_TOKEN from env. Returns empty string when
    unset. Callers should check the return and refuse to start if empty."""
    return (os.getenv(INTERNAL_SERVICE_TOKEN_ENV) or "").strip()


# === Active recorder registry ===========================================
# Maps room_id (UUID) -> RoomRecorder. Module-level so any uvicorn worker
# can look up an active recorder. In multi-worker deployments where the
# start request and the stop request land on different workers, the stop
# falls through to a no-op (the original worker's recorder shuts down
# eventually when arecord exits, but the DB state stays consistent via
# the API endpoint). Single-worker deployments are 100% covered.
_active_recorders: dict[UUID, "RoomRecorder"] = {}


def get_active_recorders() -> dict[UUID, "RoomRecorder"]:
    """Snapshot of currently-running recorders. Used by tests + status endpoints."""
    return dict(_active_recorders)


# === Helpers ============================================================


def _build_arecord_cmd(device_path: str) -> list[str]:
    """Build the arecord command line for raw 16 kHz mono S16_LE capture."""
    return [
        "arecord",
        "-D",
        device_path,
        "-f",
        "S16_LE",
        "-r",
        str(SAMPLE_RATE),
        "-c",
        str(CHANNELS),
        "-t",
        "raw",
        "--buffer-time=100000",
        "--period-time=20000",
        "-q",
    ]


def _write_wav(buffer: bytes, path: Path) -> None:
    """Wrap raw S16_LE bytes in a WAV header so the chunk endpoint can decode."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH_BYTES)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(buffer)


def _load_vad():
    """Import webrtcvad on demand. Raises a friendly error if missing."""
    try:
        import webrtcvad
    except ImportError as exc:  # pragma: no cover — webrtcvad pinned in reqs
        raise RuntimeError(
            "webrtcvad is required for RoomRecorder. Install via "
            "requirements-docker.txt (webrtcvad==2.0.10)."
        ) from exc
    return webrtcvad.Vad(2)  # moderate sensitivity


# Floor for dB conversion. -90 dB is "nominally silent" for 16-bit PCM —
# matches the noise-floor of consumer USB mics. The level-stream UI clamps
# to this so the bar bottom is stable instead of jittering near -inf.
LEVEL_DB_FLOOR = -90.0


def _samples_to_db(rms_amp: float, peak_amp: float) -> tuple[float, float]:
    """Convert linear amplitude (0..1 of full-scale int16) to dBFS."""
    rms_db = 20.0 * math.log10(rms_amp) if rms_amp > 1e-9 else LEVEL_DB_FLOOR
    peak_db = 20.0 * math.log10(peak_amp) if peak_amp > 1e-9 else LEVEL_DB_FLOOR
    return max(rms_db, LEVEL_DB_FLOOR), max(peak_db, LEVEL_DB_FLOOR)


def _pcm_s16le_stats(buf: bytes) -> tuple[float, float]:
    """Return (rms_amplitude, peak_amplitude) for raw S16_LE bytes.

    Amplitudes are normalized to [0, 1] of int16 full-scale (32768). Empty
    or partial buffers return (0, 0).

    numpy is available in the backend image but kept optional here so the
    recorder still works if numpy ever gets pulled.
    """
    if not buf or len(buf) < 2:
        return 0.0, 0.0
    try:
        import numpy as np

        samples = np.frombuffer(buf, dtype=np.int16)
        if samples.size == 0:
            return 0.0, 0.0
        # Convert once; keep float32 to bound memory on long buffers.
        x = samples.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(np.square(x))))
        peak = float(np.max(np.abs(x)))
        return rms, peak
    except ImportError:
        # Fallback: pure-Python (slower but correct).
        n = len(buf) // 2
        if n == 0:
            return 0.0, 0.0
        samples = struct.unpack(f"<{n}h", buf[: n * 2])
        sq = 0.0
        peak_i = 0
        for s in samples:
            sq += float(s) * float(s)
            ai = -s if s < 0 else s
            if ai > peak_i:
                peak_i = ai
        rms_int = (sq / n) ** 0.5
        return rms_int / 32768.0, peak_i / 32768.0


# === Recorder class =====================================================


class RoomRecorder:
    """Manages an arecord subprocess + VAD loop + chunk POSTs for one room."""

    def __init__(
        self,
        *,
        room_id: UUID,
        organization_id: int,
        organization_slug: str,
        source_id: UUID,
        source_device_path: str,
        session_pk: int,
        session_public_id: str,
        internal_token: Optional[str] = None,
    ):
        self.room_id = room_id
        self.organization_id = organization_id
        self.organization_slug = organization_slug
        self.source_id = source_id
        self.source_device_path = source_device_path
        self.session_pk = session_pk
        self.session_public_id = session_public_id
        # CR-005: shared-secret backend-internal auth. Pulled from the
        # process env at construction time (not at module import) so a
        # test can override per-instance without monkey-patching globals.
        # Empty string == feature disabled; we'll skip the POST entirely
        # and log a clear error so it surfaces in ops dashboards instead
        # of being lost to a generic 401.
        self.internal_token = (
            internal_token if internal_token is not None else _read_internal_token()
        )

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._started_at: Optional[datetime] = None
        self._elapsed_seconds: float = 0.0

        # Level-tap state. Updated on every VAD frame so the SSE endpoint
        # at /api/rooms/{id}/levels can read recent values without
        # synchronizing with the recorder loop. A short rolling window
        # smooths the bar visually — 200ms ≈ 6 frames at 30ms each.
        self._level_window_ms: int = 200
        self._level_frames_per_window: int = max(
            1, self._level_window_ms // VAD_FRAME_MS
        )
        self._level_rms_window: list[float] = []
        self._level_peak_window: list[float] = []
        self._latest_rms_db: float = LEVEL_DB_FLOOR
        self._latest_peak_db: float = LEVEL_DB_FLOOR
        self._latest_level_at: Optional[datetime] = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get_levels(self) -> dict[str, Any]:
        """Snapshot the most recent audio levels. Safe to call from any task.

        Returns dB-FS values bounded by ``LEVEL_DB_FLOOR``. If the recorder
        hasn't produced a frame yet, returns silence with a ``None``
        timestamp.
        """
        return {
            "rms_db": self._latest_rms_db,
            "peak_db": self._latest_peak_db,
            "timestamp": (
                self._latest_level_at.isoformat() if self._latest_level_at else None
            ),
            "window_ms": self._level_window_ms,
        }

    def _push_level(self, frame: bytes) -> None:
        """Update rolling RMS+peak from a freshly-read VAD frame.

        Kept cheap so it can run on every VAD frame without skewing the
        recorder's chunking timing. ~6 frames buffered (200 ms window).
        """
        rms, peak = _pcm_s16le_stats(frame)
        self._level_rms_window.append(rms)
        self._level_peak_window.append(peak)
        while len(self._level_rms_window) > self._level_frames_per_window:
            self._level_rms_window.pop(0)
        while len(self._level_peak_window) > self._level_frames_per_window:
            self._level_peak_window.pop(0)
        if not self._level_rms_window:
            return
        avg_rms = sum(self._level_rms_window) / len(self._level_rms_window)
        max_peak = max(self._level_peak_window)
        rms_db, peak_db = _samples_to_db(avg_rms, max_peak)
        self._latest_rms_db = rms_db
        self._latest_peak_db = peak_db
        self._latest_level_at = datetime.now(timezone.utc)

    async def start(self) -> None:
        """Spawn arecord and start the VAD loop. Idempotent."""
        if self.is_running:
            return
        if not self.internal_token:
            # Visible at recorder-start so an admin sees the misconfig in
            # the same log line that announces the recorder coming up,
            # instead of having to correlate a 401 with the env var name.
            logger.error(
                "RoomRecorder room=%s: %s env var is unset/empty. Audio "
                "chunks will be skipped at POST time. Set the env var on "
                "the backend container before relying on Conference Room "
                "mode.",
                self.room_id,
                INTERNAL_SERVICE_TOKEN_ENV,
            )
        cmd = _build_arecord_cmd(self.source_device_path)
        logger.info(
            "Starting RoomRecorder room=%s source=%s cmd=%s",
            self.room_id,
            self.source_device_path,
            " ".join(cmd),
        )
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._started_at = datetime.now(timezone.utc)
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_loop(),
            name=f"room-recorder-{self.room_id}",
        )

    async def stop(self) -> None:
        """Terminate arecord and finalize."""
        self._stop_event.set()
        proc = self._proc
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
        if self._task and not self._task.done():
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
        logger.info("Stopped RoomRecorder room=%s", self.room_id)

    async def _run_loop(self) -> None:
        """Read raw bytes from arecord stdout, run VAD, flush chunks."""
        if not self._proc or not self._proc.stdout:
            return
        try:
            vad = _load_vad()
        except RuntimeError:
            logger.exception("VAD load failed for room=%s", self.room_id)
            await self._mark_error("VAD unavailable")
            return

        stdout = self._proc.stdout
        buffer = bytearray()  # current chunk being assembled
        trailing_silence = 0
        speech_frames_in_chunk = 0
        frames_in_chunk = 0

        while not self._stop_event.is_set():
            try:
                frame = await stdout.readexactly(VAD_FRAME_BYTES)
            except asyncio.IncompleteReadError as exc:
                # arecord exited. Flush whatever we have and bail.
                if exc.partial:
                    buffer.extend(exc.partial)
                logger.warning(
                    "arecord stream ended unexpectedly for room=%s (partial=%dB)",
                    self.room_id,
                    len(exc.partial),
                )
                await self._flush_chunk(bytes(buffer), speech_frames_in_chunk)
                if self._proc and self._proc.returncode is not None and self._proc.returncode != 0:
                    stderr = await self._proc.stderr.read() if self._proc.stderr else b""
                    await self._mark_error(
                        f"arecord exited rc={self._proc.returncode}: {stderr.decode(errors='ignore')[:300]}"
                    )
                return
            except Exception:
                logger.exception("Unexpected error reading arecord stdout (room=%s)", self.room_id)
                return

            is_speech = False
            try:
                is_speech = vad.is_speech(frame, SAMPLE_RATE)
            except Exception:
                # Bad frame size or VAD freakout — treat as silence.
                is_speech = False

            # Update the rolling level window before we touch the chunk
            # buffer — keeps the VU meter responsive regardless of how
            # the chunk ends up being assembled.
            self._push_level(frame)

            buffer.extend(frame)
            frames_in_chunk += 1
            if is_speech:
                trailing_silence = 0
                speech_frames_in_chunk += 1
            else:
                trailing_silence += 1

            # End-of-turn condition.
            end_of_turn = (
                speech_frames_in_chunk > 0
                and trailing_silence >= MAX_TRAILING_NON_SPEECH_FRAMES
            )
            hard_cap = frames_in_chunk >= MAX_CHUNK_FRAMES

            if end_of_turn or hard_cap:
                # Don't bother POSTing micro-blips.
                if speech_frames_in_chunk >= MIN_CHUNK_FRAMES:
                    chunk_bytes = bytes(buffer)
                    await self._flush_chunk(chunk_bytes, speech_frames_in_chunk)
                # Reset chunk state regardless (drop micro-blips outright).
                buffer = bytearray()
                trailing_silence = 0
                speech_frames_in_chunk = 0
                frames_in_chunk = 0

        # Stop requested — flush remaining audio if it contains speech.
        if speech_frames_in_chunk >= MIN_CHUNK_FRAMES:
            await self._flush_chunk(bytes(buffer), speech_frames_in_chunk)

    async def _flush_chunk(self, raw_bytes: bytes, speech_frames: int) -> None:
        """Encode a WAV and POST to the chunks endpoint."""
        if not raw_bytes:
            return
        duration_seconds = len(raw_bytes) / (SAMPLE_RATE * SAMPLE_WIDTH_BYTES * CHANNELS)
        elapsed_seconds = self._elapsed_seconds
        self._elapsed_seconds += duration_seconds

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = Path(tmp.name)
        try:
            _write_wav(raw_bytes, wav_path)
            await self._post_chunk(wav_path, duration_seconds, elapsed_seconds)
        except Exception:
            logger.exception(
                "Chunk POST failed for room=%s session=%s",
                self.room_id,
                self.session_public_id,
            )
        finally:
            try:
                wav_path.unlink(missing_ok=True)
            except Exception:
                pass

    async def _post_chunk(
        self,
        wav_path: Path,
        duration_seconds: float,
        elapsed_seconds: float,
    ) -> None:
        """POST the chunk to the existing /chunks endpoint.

        Falls back to /chunks-text if /chunks ever returns 405/501 (the
        chunk POST is the primary path; the text endpoint is only used if
        the recorder ever runs in transcript-only mode, which Phase 1
        doesn't).
        """
        if not self.internal_token:
            # Fail-fast (per-chunk). The token is required for the loopback
            # POST to clear the dual-auth dependency; without it every
            # chunk would 401 and the audio would silently disappear.
            logger.error(
                "RoomRecorder room=%s: %s env var is unset/empty; chunk "
                "POST skipped. Audio will not be persisted. Set the env "
                "var on the backend container and recreate.",
                self.room_id,
                INTERNAL_SERVICE_TOKEN_ENV,
            )
            return

        url = (
            f"{INTERNAL_API_BASE}/api/recordings/sessions/"
            f"{self.session_public_id}/chunks"
        )
        headers: dict[str, str] = {
            "X-MeetingOps-Org": self.organization_slug,
            "X-Room-Recorder": "1",
            "X-Room-Recorder-Provenance": PROVENANCE_TAG,
            # Backend-internal shared secret. Accepted by the chunks
            # endpoints' dual-auth dependency in place of a user JWT.
            # Never logged in full; never returned to clients.
            INTERNAL_TOKEN_HEADER: self.internal_token,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            with wav_path.open("rb") as f:
                files = {"chunk": (wav_path.name, f, "audio/wav")}
                data = {
                    "elapsed_seconds": f"{elapsed_seconds:.3f}",
                }
                resp = await client.post(url, headers=headers, files=files, data=data)
            if resp.status_code >= 400:
                logger.warning(
                    "Chunk POST returned %s for room=%s: %s",
                    resp.status_code,
                    self.room_id,
                    resp.text[:300],
                )

    async def _mark_error(self, message: str) -> None:
        """Flip the room/session/source to error and remove ourselves
        from the active registry."""
        logger.error("RoomRecorder error room=%s: %s", self.room_id, message)
        db = SessionLocal()
        try:
            session = (
                db.query(RecordingSession)
                .filter(RecordingSession.id == self.session_pk)
                .first()
            )
            if session and session.status in {"recording", "processing", "paused"}:
                session.status = "error"
                metadata = dict(session.processing_metadata or {})
                metadata.setdefault("errors", []).append(
                    {
                        "at": datetime.now(timezone.utc).isoformat(),
                        "source": "room_recorder",
                        "message": message,
                    }
                )
                session.processing_metadata = metadata
            room = (
                db.query(ConferenceRoom)
                .filter(ConferenceRoom.id == self.room_id)
                .first()
            )
            if room:
                room.status = "error"
            source = (
                db.query(RoomAudioSource)
                .filter(RoomAudioSource.id == self.source_id)
                .first()
            )
            if source:
                source.status = "error"
            db.commit()
        finally:
            db.close()
        _active_recorders.pop(self.room_id, None)


# === Public service API =================================================


async def start_room_recorder(
    *,
    room: ConferenceRoom,
    source: RoomAudioSource,
    session: RecordingSession,
) -> RoomRecorder:
    """Start a recorder for this room. Raises if already running.

    Resolves the org slug eagerly because the recorder holds it for the
    lifetime of the session (used in the X-MeetingOps-Org header on every
    chunk POST so the chunks endpoint resolves the correct org).
    """
    if room.id in _active_recorders:
        raise RuntimeError(f"Room {room.id} already has an active recorder")

    if source.hardware_type != "server_usb_mic":
        # Phase 1 only ships the server USB mic path. Satellites + companion
        # apps post chunks themselves via existing endpoints in Phase 3.
        raise RuntimeError(
            f"Phase 1 RoomRecorder only supports server_usb_mic, got {source.hardware_type}"
        )
    if not source.device_path:
        raise RuntimeError("server_usb_mic source missing device_path")

    db = SessionLocal()
    try:
        org_slug = (
            db.query(ConferenceRoom)
            .filter(ConferenceRoom.id == room.id)
            .first()
        )
        # Resolve slug via the Organization table.
        from auth.models import Organization

        org = (
            db.query(Organization)
            .filter(Organization.id == room.organization_id)
            .first()
        )
        if not org:
            raise RuntimeError(f"Organization {room.organization_id} not found")
        organization_slug = org.slug
    finally:
        db.close()

    recorder = RoomRecorder(
        room_id=room.id,
        organization_id=room.organization_id,
        organization_slug=organization_slug,
        source_id=source.id,
        source_device_path=source.device_path,
        session_pk=session.id,
        session_public_id=session.session_id or str(session.id),
    )
    _active_recorders[room.id] = recorder
    try:
        await recorder.start()
    except Exception:
        _active_recorders.pop(room.id, None)
        raise
    return recorder


async def stop_room_recorder(*, room_id: UUID) -> bool:
    """Stop a room's recorder. Returns True if a recorder was active.

    Idempotent — calling stop on an already-stopped room is a no-op."""
    recorder = _active_recorders.pop(room_id, None)
    if not recorder:
        return False
    await recorder.stop()
    return True
