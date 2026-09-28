"""meet-parakeet-stream-svc — real Parakeet 0.6B v3 streaming-ish ASR.

Phase B.2 of the server-live pipeline. This service is the upstream the
meet-backend WS handler in ``backend/api/streaming.py`` POSTs chunked
audio windows to. It returns the transcript for the window as JSON.

Endpoints
---------
GET  /healthz                — liveness + model-loaded flag.
GET  /readyz                 — same shape, semantic alias.
POST /transcribe-stream      — v1: accept audio bytes (WAV / raw PCM16),
                               return ``{text, segments, confidence, model,
                               rtf, duration, sequence}``. Stateless per call.
POST /transcribe-stream-v2   — Phase B.3 spike: session-stateful endpoint
                               returning ``{tokens_finalized, tokens_draft,
                               text_finalized, text_draft, sequence,
                               is_final}``. See
                               ``docs/phase-b3-nemo-streaming-spike.md`` for
                               the design rationale and why this is not
                               cache-aware native streaming.

The container layers on top of the existing ``meet-parakeet-svc:local``
image so we inherit NeMo 2.4.1 + torch 2.4.1 + CUDA 12.4 + audio tooling.
The only differences from parakeet-svc are:
  - default model is ``nvidia/parakeet-tdt-0.6b-v3`` (the 2026-vintage
    multilingual 0.6 B variant we use for the streaming path), with a
    fallback to ``nvidia/parakeet-tdt-0.6b-v2``,
  - endpoint shape matches what the meet-backend ws handler expects
    (binary body + per-call ``X-Session-Id`` / ``X-Flush-Sequence`` headers
    for correlation),
  - we do **not** do whole-meeting chunking — every POST is already a
    short window from the WS handler.

This is "near-streaming": each window is a small offline transcribe call,
which is good enough for sub-1.5 s p95 partial latency at our 2.5 s
window cadence. True NeMo native streaming (NeMo's `conformer_stream_step`
cache-aware path) was prototyped under Phase B.3 and found to **not work
on this checkpoint** — `parakeet-tdt-0.6b-v3` was trained with
`att_context_size=[-1,-1]` (full context), so the streaming forward
returns junk after the first chunk. The v2 endpoint below is the
practical compromise: stateful pseudo-streaming with proper
draft+finalize token semantics, against the same checkpoint.
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import numpy as np
import soundfile as sf
import torch
import torchaudio
from fastapi import FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger("parakeet-stream-svc")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
TARGET_SR = 16000

# The 0.6B v3 (multilingual, 2026-vintage) is the canonical streaming
# variant per docs/phase-b-server-live-streaming.md. v2 is the fallback —
# it's English-only but ships ~6 weeks earlier so HF cache hits are
# common in dev. The fp32 0.6B family sits ~2.4 GB resident, fp16 ~1.4 GB
# resident on the 3060 alongside the existing 1.1B batch container (~4 GB).
DEFAULT_MODEL = os.getenv(
    "PARAKEET_STREAM_MODEL", "nvidia/parakeet-tdt-0.6b-v3"
)
FALLBACK_MODEL = os.getenv(
    "PARAKEET_STREAM_FALLBACK_MODEL", "nvidia/parakeet-tdt-0.6b-v2"
)
MODEL_PRECISION = os.getenv("PARAKEET_STREAM_PRECISION", "fp16").strip().lower()

# Window sizing safety rails. Real windows from the WS handler are 2.5 s
# nominal; we reject anything wildly outside the expected range so a
# misbehaving client can't pin the GPU on a 10-min clip on the streaming
# port. The batch endpoint at parakeet-svc:8881 is the right surface
# for long audio.
MIN_AUDIO_SECONDS = float(os.getenv("STREAM_MIN_AUDIO_SECONDS", "0.05"))
MAX_AUDIO_SECONDS = float(os.getenv("STREAM_MAX_AUDIO_SECONDS", "30.0"))

SERVICE_VERSION = "0.3.0-b3spike"  # B.3 spike: adds /transcribe-stream-v2.

# ---------- Phase B.3 streaming-v2 knobs ----------
# Audio buffer per session (seconds). The v2 endpoint keeps a rolling ring
# of audio per session-id and re-transcribes the tail on each call so the
# model has enough left-context to land stable token boundaries. 6s gives
# us ~3-5 finalized tokens of "trailing context" per call which empirically
# is enough for the LCS / token-stability heuristic to lock the leading
# tokens as finalized.
STREAM_V2_BUFFER_SECONDS = float(os.getenv("STREAM_V2_BUFFER_SECONDS", "6.0"))
# Tail of the buffer (in seconds) considered "draft" — i.e. tokens emitted
# from this region are still subject to revision on the next call. Tokens
# before the (buffer - tail) cutoff are eligible to be promoted to
# finalized. 2.0s ~ 4-7 tokens of draft at parakeet's ~3 tok/s rate.
STREAM_V2_DRAFT_TAIL_SECONDS = float(os.getenv("STREAM_V2_DRAFT_TAIL_SECONDS", "2.0"))
# Cap on how many sessions we keep state for. Each session holds at most
# STREAM_V2_BUFFER_SECONDS * 16000 * 4 bytes (fp32) ~ 384 KB at 6s, so 256
# sessions ~ 96 MB. Easily fits.
STREAM_V2_MAX_SESSIONS = int(os.getenv("STREAM_V2_MAX_SESSIONS", "256"))
# Idle timeout after which a session's state is evicted (seconds). Default
# 10 min — comfortably longer than any meeting's idle gap.
STREAM_V2_SESSION_TTL_SECONDS = float(os.getenv("STREAM_V2_SESSION_TTL_SECONDS", "600.0"))

# Module-level model state — single instance, kept warm across calls.
_model: Any = None
_model_name: str = ""
_model_load_error: Optional[str] = None
_model_load_seconds: Optional[float] = None


def _load_model() -> Any:
    """Eager-load Parakeet 0.6B v3 at startup. Tries DEFAULT_MODEL first,
    falls back to FALLBACK_MODEL on any exception. Idempotent; cheap to
    call repeatedly once warm.
    """
    global _model, _model_name, _model_load_error, _model_load_seconds
    if _model is not None:
        return _model

    from nemo.collections.asr.models import ASRModel  # deferred import

    candidates = [DEFAULT_MODEL]
    if FALLBACK_MODEL and FALLBACK_MODEL != DEFAULT_MODEL:
        candidates.append(FALLBACK_MODEL)

    last_exc: Optional[Exception] = None
    for name in candidates:
        try:
            logger.info("Loading Parakeet stream model %s on %s", name, DEVICE)
            t0 = time.time()
            model = ASRModel.from_pretrained(model_name=name)
            model = model.to(DEVICE)
            if MODEL_PRECISION in ("fp16", "half", "float16"):
                if DEVICE == "cuda":
                    model = model.half()
                    logger.info("Parakeet stream precision = fp16")
                else:
                    logger.warning("Ignoring fp16 precision on non-CUDA device")
            try:
                model.eval()
            except Exception:
                pass
            _model = model
            _model_name = name
            _model_load_error = None
            _model_load_seconds = time.time() - t0
            logger.info(
                "Parakeet stream ready: %s (load took %.1f s)",
                name, _model_load_seconds,
            )
            return _model
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            err = f"{type(exc).__name__}: {exc}"
            logger.warning("Failed to load %s: %s", name, err)
            _model_load_error = err
            _model = None
            if torch.cuda.is_available():
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass

    msg = f"All Parakeet stream candidates failed; last error: {last_exc}"
    logger.error(msg)
    _model_load_error = str(last_exc) if last_exc else "unknown error"
    raise RuntimeError(msg)


def _decode_audio(buf: bytes) -> tuple[np.ndarray, float]:
    """Accept raw bytes that should be WAV-wrapped audio (what the
    meet-backend WS handler sends after wrapping PCM16 in a RIFF header).

    Falls back to treating the input as raw PCM16 at 16 kHz mono if WAV
    parsing fails — the streaming path is fast and we'd rather emit a
    best-effort transcript than 400.

    Returns (mono float32 waveform at TARGET_SR, duration seconds).
    """
    wav: Optional[np.ndarray] = None
    sr: int = 0

    # Try WAV first.
    try:
        wav, sr = sf.read(io.BytesIO(buf), dtype="float32", always_2d=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("WAV parse failed (%s); trying raw PCM16", exc)

    # Raw PCM16 fallback. The WS handler always wraps in WAV today, but
    # this keeps us forgiving of clients that POST raw PCM directly during
    # B.2 manual tests.
    if wav is None or wav.size == 0:
        try:
            i16 = np.frombuffer(buf, dtype=np.int16)
            wav = (i16.astype(np.float32) / 32768.0)
            sr = TARGET_SR
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"audio decode failed (not WAV, not PCM16): {exc}",
            )

    if wav.ndim > 1:
        wav = wav.mean(axis=1)

    if sr != TARGET_SR:
        wav_t = torch.from_numpy(wav).unsqueeze(0)
        wav_t = torchaudio.functional.resample(wav_t, sr, TARGET_SR)
        wav = wav_t.squeeze(0).numpy()

    duration = float(len(wav)) / float(TARGET_SR)
    return wav, duration


def _transcribe_window(wav: np.ndarray) -> dict[str, Any]:
    """Run one short-window transcribe via NeMo. Returns a dict with
    text + per-segment list + the model name used. RTF + confidence are
    set by the caller (we need elapsed wall-clock from the request entry).
    """
    model = _load_model()

    # NeMo's transcribe() takes a list of file paths today. The cheapest
    # way to feed in-memory audio is via a tempfile WAV — soundfile writes
    # PCM16 in ~1 ms for our window sizes.
    fd, path = tempfile.mkstemp(prefix="parakeet_stream_", suffix=".wav")
    os.close(fd)
    try:
        sf.write(path, wav, TARGET_SR, subtype="PCM_16")
        try:
            results = model.transcribe(
                [path], timestamps=True, batch_size=1,
            )
        except TypeError:
            # Older NeMo signature variant — fall back.
            try:
                results = model.transcribe(
                    [path], return_hypotheses=True, batch_size=1,
                )
            except TypeError:
                results = model.transcribe([path])

        if isinstance(results, tuple):
            results = results[0]
        if not results:
            return {"text": "", "segments": [], "words": [], "model": _model_name}

        hyp = results[0]
        text = ""
        segments: list[dict[str, Any]] = []
        words: list[dict[str, Any]] = []

        if isinstance(hyp, str):
            text = hyp.strip()
        elif hasattr(hyp, "text"):
            text = (hyp.text or "").strip()
            ts = getattr(hyp, "timestamp", None) or {}
            seg_ts = ts.get("segment") if isinstance(ts, dict) else None
            if seg_ts:
                for s in seg_ts:
                    segments.append({
                        "start": float(s.get("start", 0.0)),
                        "end": float(s.get("end", 0.0)),
                        "text": (s.get("segment") or s.get("text") or "").strip(),
                        "confidence": float(s.get("confidence", 0.95)),
                        "speaker": None,
                    })
            word_ts = ts.get("word") if isinstance(ts, dict) else None
            if word_ts:
                for w in word_ts:
                    words.append({
                        "start": float(w.get("start", 0.0)),
                        "end": float(w.get("end", 0.0)),
                        "word": (w.get("word") or w.get("text") or "").strip(),
                        "confidence": float(w.get("confidence", 0.97)),
                    })
        else:
            text = str(hyp).strip()

        return {
            "text": text,
            "segments": segments,
            "words": words,
            "model": _model_name,
        }
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _transcribe_window_with_words(wav: np.ndarray) -> dict[str, Any]:
    """Same as _transcribe_window but always asks NeMo for word timestamps.

    The v2 endpoint needs word-level granularity to compute draft/finalize
    boundaries; segment-level wouldn't work because a segment spans the
    whole window and we'd never finalize anything until the session ends.
    """
    model = _load_model()
    fd, path = tempfile.mkstemp(prefix="parakeet_stream_v2_", suffix=".wav")
    os.close(fd)
    try:
        sf.write(path, wav, TARGET_SR, subtype="PCM_16")
        # timestamps=True asks NeMo for word- and segment-level timing.
        try:
            results = model.transcribe([path], timestamps=True, batch_size=1)
        except TypeError:
            results = model.transcribe([path], return_hypotheses=True, batch_size=1)

        if isinstance(results, tuple):
            results = results[0]
        if not results:
            return {"text": "", "words": [], "model": _model_name}

        hyp = results[0]
        text = ""
        words: list[dict[str, Any]] = []

        if hasattr(hyp, "text"):
            text = (hyp.text or "").strip()
            ts = getattr(hyp, "timestamp", None) or {}
            word_ts = ts.get("word") if isinstance(ts, dict) else None
            if word_ts:
                for w in word_ts:
                    words.append({
                        "start": float(w.get("start", 0.0)),
                        "end": float(w.get("end", 0.0)),
                        "word": (w.get("word") or w.get("text") or "").strip(),
                        "confidence": float(w.get("confidence", 0.97)),
                    })
        else:
            text = str(hyp).strip() if hyp else ""

        return {"text": text, "words": words, "model": _model_name}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------- request / response shapes ----------

class HealthResponse(BaseModel):
    status: str
    version: str
    model_loaded: bool
    model: Optional[str] = None
    requested_model: str = DEFAULT_MODEL
    fallback_model: Optional[str] = FALLBACK_MODEL
    load_error: Optional[str] = None
    load_seconds: Optional[float] = None
    device: str = DEVICE
    cuda_available: bool = False
    precision: str = MODEL_PRECISION
    phase: str = "B.3-spike"


class TranscribeSegment(BaseModel):
    start: float
    end: float
    text: str
    confidence: float = 0.95
    speaker: Optional[str] = None


class TranscribeWord(BaseModel):
    start: float
    end: float
    word: str
    confidence: float = 0.97


class TranscribeStreamResponse(BaseModel):
    text: str
    segments: list[TranscribeSegment] = Field(default_factory=list)
    words: list[TranscribeWord] = Field(default_factory=list)
    duration: float
    model: str
    rtf: float
    confidence: float = 0.95
    sequence: Optional[int] = None
    is_final: bool = False
    service_version: str = SERVICE_VERSION


# ---------- routes ----------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the model at startup so the first /transcribe-stream isn't
    # gated on ~20 s of NeMo init. If load fails we still come up so
    # /healthz can report the error.
    try:
        _load_model()
    except Exception as exc:  # noqa: BLE001
        logger.error("Startup model load failed: %s", exc)
    yield


app = FastAPI(
    title="meet-parakeet-stream-svc",
    version=SERVICE_VERSION,
    description=(
        "Phase B.2 streaming-ish ASR using NeMo Parakeet 0.6B v3 (or v2 "
        "fallback). Counterpart to the batch parakeet-svc at :8881."
    ),
    lifespan=lifespan,
)


@app.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    cuda = torch.cuda.is_available()
    status = "ok" if _model is not None else ("loading" if _model_load_error is None else "degraded")
    return HealthResponse(
        status=status,
        version=SERVICE_VERSION,
        model_loaded=_model is not None,
        model=_model_name or None,
        requested_model=DEFAULT_MODEL,
        fallback_model=FALLBACK_MODEL,
        load_error=_model_load_error,
        load_seconds=_model_load_seconds,
        device=DEVICE,
        cuda_available=cuda,
        precision=MODEL_PRECISION,
        phase="B.3-spike",
    )


@app.get("/readyz", response_model=HealthResponse)
def readyz() -> HealthResponse:
    """Same body as /healthz; convention alias."""
    return healthz()


@app.post("/transcribe-stream", response_model=TranscribeStreamResponse)
async def transcribe_stream(
    request: Request,
    return_word_timestamps: bool = Query(default=False),
    x_session_id: Optional[str] = Header(default=None, alias="X-Session-Id"),
    x_flush_sequence: Optional[str] = Header(default=None, alias="X-Flush-Sequence"),
    x_is_final: Optional[str] = Header(default=None, alias="X-Is-Final"),
) -> TranscribeStreamResponse:
    """Transcribe a single short audio window.

    Request body: WAV bytes (preferred — meet-backend WS handler wraps
    PCM16 in a minimal RIFF) or raw PCM16 LE at 16 kHz mono.

    Headers (all optional):
      X-Session-Id      — opaque per-WS session identifier for log correlation
      X-Flush-Sequence  — per-session monotonic counter from the WS handler
      X-Is-Final        — "1" if this is the last window of the segment

    Response: text + (optional) word/segment timestamps + the actual model
    used + the realtime factor for this window.
    """
    started = time.time()

    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio body")

    try:
        seq_i: Optional[int] = int(x_flush_sequence) if x_flush_sequence else None
    except ValueError:
        seq_i = None
    is_final = (x_is_final == "1")

    wav, duration = _decode_audio(raw)
    if duration < MIN_AUDIO_SECONDS:
        # Empty / sub-frame buffer — return an empty transcript rather
        # than 400 so the WS handler's de-dup logic stays simple.
        return TranscribeStreamResponse(
            text="",
            duration=duration,
            model=_model_name or DEFAULT_MODEL,
            rtf=0.0,
            confidence=0.0,
            sequence=seq_i,
            is_final=is_final,
        )
    if duration > MAX_AUDIO_SECONDS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"window too long ({duration:.1f}s > {MAX_AUDIO_SECONDS:.0f}s) — "
                "use the batch /transcribe endpoint on parakeet-svc:8881 for "
                "full meetings"
            ),
        )

    if _model is None and _model_load_error is None:
        # Still warming up; tell caller it's transient.
        raise HTTPException(
            status_code=503,
            detail="model still loading — retry in a few seconds",
        )

    if _model is None:
        raise HTTPException(
            status_code=503,
            detail=f"model load failed: {_model_load_error}",
        )

    try:
        result = _transcribe_window(wav)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("transcribe failed session=%s seq=%s", x_session_id, seq_i)
        raise HTTPException(status_code=500, detail=f"transcribe failed: {exc}")

    elapsed = time.time() - started
    rtf = elapsed / duration if duration > 0 else 0.0

    text = result.get("text", "") or ""
    segments = result.get("segments") or []
    words = result.get("words") or [] if return_word_timestamps else []
    # If the model gave text but no segments (some NeMo configs), synthesize
    # a single segment covering the whole window. This keeps the response
    # shape stable for the WS handler regardless of whether timestamps were
    # produced.
    if not segments and text:
        segments = [{
            "start": 0.0,
            "end": duration,
            "text": text,
            "confidence": 0.95,
            "speaker": None,
        }]

    # Confidence: NeMo doesn't surface a single-window confidence directly.
    # We mean-pool whatever segment-level numbers we got (defaults to 0.95
    # if none). The meet-backend WS handler passes this through verbatim.
    if segments:
        confs = [float(s.get("confidence", 0.95)) for s in segments]
        confidence = sum(confs) / len(confs) if confs else 0.95
    else:
        confidence = 0.0 if not text else 0.95

    logger.info(
        "/transcribe-stream session=%s seq=%s duration=%.2fs elapsed=%.2fs "
        "rtf=%.3f chars=%d final=%s",
        x_session_id, seq_i, duration, elapsed, rtf, len(text), is_final,
    )

    return TranscribeStreamResponse(
        text=text,
        segments=[TranscribeSegment(**s) for s in segments],
        words=[TranscribeWord(**w) for w in words],
        duration=duration,
        model=_model_name or DEFAULT_MODEL,
        rtf=rtf,
        confidence=float(confidence),
        sequence=seq_i,
        is_final=is_final,
    )


# ====================================================================
# Phase B.3 spike: /transcribe-stream-v2 with draft+finalize semantics.
# ====================================================================
#
# Why this exists vs the v1 endpoint above:
#   v1 is stateless — the meet-backend WS handler accumulates audio
#   locally, slices ~2.5s windows, and POSTs each window independently.
#   The response is the full transcript of that window. The handler then
#   does its own de-dup against the previous window's text. This works
#   but the "stable" portion of the transcript is the WS handler's
#   responsibility to track.
#
# v2 moves that responsibility into the ASR service. The contract:
#   - Client opens a session by POSTing the first chunk with
#     X-Session-Id: <opaque-id> + X-Flush-Sequence: 0.
#   - Each subsequent chunk uses the same X-Session-Id and an incremented
#     X-Flush-Sequence.
#   - Service maintains a per-session audio ring buffer
#     (STREAM_V2_BUFFER_SECONDS) + the most recent transcription's word
#     list + a "promoted to finalized" word list.
#   - On each call, we append the new audio to the ring, run
#     transcribe(timestamps=True) on the ring contents, then partition
#     the resulting words into:
#       * tokens_finalized: words whose end-time is < (ring_duration -
#         STREAM_V2_DRAFT_TAIL_SECONDS). These are stable — the same
#         word will be emitted again on subsequent calls because the
#         underlying audio context is unchanged.
#       * tokens_draft: words whose end-time is >= the cutoff. These
#         are subject to revision on the next call as more context
#         lands.
#   - Words promoted from draft to finalized between calls are also
#     persisted in session state so the SAME word doesn't get emitted as
#     finalized on every call (the client only ever sees the new
#     finalized words on each response).
#   - On is_final=1 the entire transcript becomes finalized and the
#     session state is freed.
#
# Why this is NOT NeMo's cache-aware streaming protocol (what the
# original Phase B.3 design intended): see the spike report at
# docs/phase-b3-nemo-streaming-spike.md. Short version: the
# parakeet-tdt-0.6b-v3 checkpoint was trained with full-context
# attention, so the conformer_stream_step() forward returns junk after
# the first chunk. The v2 endpoint here is what we ship until we can
# swap to a streaming-trained model variant.


class StreamSessionState:
    """Per-session rolling state for /transcribe-stream-v2.

    audio_ring: float32 mono @ 16 kHz, length <= STREAM_V2_BUFFER_SECONDS * sr.
    samples_consumed_before_ring: how many samples were dropped from the
        head of the ring (so we can map ring-relative timestamps back to
        session-absolute time).
    finalized_words: list of {word, start, end, confidence} that have
        been promoted to finalized and already returned to the client.
        The next call will only emit the *new* additions.
    last_seen: monotonic time of last call, for eviction.
    """
    __slots__ = (
        "audio_ring",
        "samples_consumed_before_ring",
        "finalized_words",
        "last_seen",
        "first_seq",
    )

    def __init__(self) -> None:
        self.audio_ring: np.ndarray = np.zeros(0, dtype=np.float32)
        self.samples_consumed_before_ring: int = 0
        self.finalized_words: list[dict[str, Any]] = []
        self.last_seen: float = time.time()
        self.first_seq: Optional[int] = None


# Module-global session registry. Keyed by X-Session-Id.
_sessions: dict[str, StreamSessionState] = {}


def _evict_idle_sessions() -> int:
    """Drop sessions older than STREAM_V2_SESSION_TTL_SECONDS. Returns
    the number evicted. Called opportunistically from the v2 endpoint.
    """
    now = time.time()
    stale = [
        sid for sid, st in _sessions.items()
        if now - st.last_seen > STREAM_V2_SESSION_TTL_SECONDS
    ]
    for sid in stale:
        _sessions.pop(sid, None)
    # Hard cap if we're way over the session limit (DOS protection).
    if len(_sessions) > STREAM_V2_MAX_SESSIONS:
        # Drop the oldest until we're under the cap.
        ordered = sorted(_sessions.items(), key=lambda kv: kv[1].last_seen)
        overflow = len(_sessions) - STREAM_V2_MAX_SESSIONS
        for sid, _ in ordered[:overflow]:
            _sessions.pop(sid, None)
            stale.append(sid)
    return len(stale)


def _append_to_ring(state: StreamSessionState, new_wav: np.ndarray) -> None:
    """Append new audio to the session ring buffer, evicting from the
    head if we exceed the buffer size. Updates samples_consumed_before_ring
    so timestamps can be mapped to session-absolute time later.
    """
    max_samples = int(STREAM_V2_BUFFER_SECONDS * TARGET_SR)
    combined = np.concatenate([state.audio_ring, new_wav.astype(np.float32)])
    if len(combined) > max_samples:
        drop = len(combined) - max_samples
        state.samples_consumed_before_ring += drop
        combined = combined[drop:]
    state.audio_ring = combined


class StreamWordToken(BaseModel):
    word: str
    start: float  # session-absolute time in seconds
    end: float
    confidence: float = 0.95


class TranscribeStreamV2Response(BaseModel):
    # The newly-promoted finalized words from THIS call only. Cumulative
    # finalized state lives on the client (or is reconstructible by
    # concatenating across responses). This keeps the response small.
    tokens_finalized: list[StreamWordToken] = Field(default_factory=list)
    # All draft words currently emitted from the ring's tail. These are
    # the unstable suffix; the client should display them but expect
    # revisions.
    tokens_draft: list[StreamWordToken] = Field(default_factory=list)
    # Convenience text renderings.
    text_finalized: str = ""
    text_draft: str = ""
    # Bookkeeping.
    sequence: Optional[int] = None
    is_final: bool = False
    session_id: Optional[str] = None
    ring_duration: float = 0.0
    session_audio_duration: float = 0.0
    elapsed_ms: float = 0.0
    rtf: float = 0.0
    model: str = ""
    service_version: str = SERVICE_VERSION


def _split_words_into_finalized_draft(
    words: list[dict[str, Any]],
    ring_duration: float,
    samples_consumed_before_ring: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition the word list (timestamps relative to the ring start)
    into (finalized, draft). Finalized = words whose end-time is in the
    "stable" prefix of the ring; draft = words in the tail.

    Also rewrites timestamps to be SESSION-absolute (i.e. adds the offset
    for samples already evicted from the ring head).
    """
    if not words:
        return [], []

    draft_cutoff = max(0.0, ring_duration - STREAM_V2_DRAFT_TAIL_SECONDS)
    head_offset = samples_consumed_before_ring / float(TARGET_SR)

    finalized: list[dict[str, Any]] = []
    draft: list[dict[str, Any]] = []
    for w in words:
        end_ring = float(w.get("end", 0.0))
        # Promote timestamps to session-absolute.
        w_abs = {
            "word": w.get("word", ""),
            "start": float(w.get("start", 0.0)) + head_offset,
            "end": end_ring + head_offset,
            "confidence": float(w.get("confidence", 0.95)),
        }
        if end_ring <= draft_cutoff:
            finalized.append(w_abs)
        else:
            draft.append(w_abs)
    return finalized, draft


def _words_equal(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Stability comparison for promoting draft -> finalized. Word equality
    on text + start time match within 100ms tolerance (NeMo's word
    timestamps wobble a bit between calls because each call sees a
    different audio context). 100ms is a generous tolerance — Parakeet
    word boundaries are far tighter than that in practice.
    """
    if a.get("word", "").strip() != b.get("word", "").strip():
        return False
    return abs(float(a.get("start", 0)) - float(b.get("start", 0))) < 0.1


@app.post("/transcribe-stream-v2", response_model=TranscribeStreamV2Response)
async def transcribe_stream_v2(
    request: Request,
    x_session_id: Optional[str] = Header(default=None, alias="X-Session-Id"),
    x_flush_sequence: Optional[str] = Header(default=None, alias="X-Flush-Sequence"),
    x_is_final: Optional[str] = Header(default=None, alias="X-Is-Final"),
) -> TranscribeStreamV2Response:
    """Phase B.3 spike: session-stateful streaming with draft+finalize.

    See the file-level docstring for the contract. In short: feed me
    consecutive chunks from the same X-Session-Id and I'll return the
    *newly-finalized* words on each call plus the current draft tail.
    """
    started = time.time()

    if not x_session_id:
        raise HTTPException(
            status_code=400,
            detail="X-Session-Id header is required for /transcribe-stream-v2",
        )

    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio body")

    try:
        seq_i: Optional[int] = int(x_flush_sequence) if x_flush_sequence else None
    except ValueError:
        seq_i = None
    is_final = (x_is_final == "1")

    wav, duration = _decode_audio(raw)
    if duration > MAX_AUDIO_SECONDS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"chunk too long ({duration:.1f}s > {MAX_AUDIO_SECONDS:.0f}s) — "
                "v2 expects small (~200ms-2.5s) chunks per call"
            ),
        )

    if _model is None and _model_load_error is None:
        raise HTTPException(status_code=503, detail="model still loading")
    if _model is None:
        raise HTTPException(
            status_code=503,
            detail=f"model load failed: {_model_load_error}",
        )

    # Opportunistic eviction. O(n) over sessions but n <= 256 so fine.
    _evict_idle_sessions()

    # Get or create session state.
    state = _sessions.get(x_session_id)
    if state is None:
        state = StreamSessionState()
        state.first_seq = seq_i
        _sessions[x_session_id] = state

    # Append the new audio.
    _append_to_ring(state, wav)
    state.last_seen = time.time()

    ring_duration = len(state.audio_ring) / float(TARGET_SR)
    session_audio_duration = (
        state.samples_consumed_before_ring + len(state.audio_ring)
    ) / float(TARGET_SR)

    # If we have less than 100ms of audio, just return empty — wait for
    # more context. Helps with the first 1-2 calls where the audio is
    # too short for the model to produce anything meaningful.
    if ring_duration < 0.1:
        return TranscribeStreamV2Response(
            tokens_finalized=[],
            tokens_draft=[],
            sequence=seq_i,
            is_final=is_final,
            session_id=x_session_id,
            ring_duration=ring_duration,
            session_audio_duration=session_audio_duration,
            elapsed_ms=(time.time() - started) * 1000.0,
            rtf=0.0,
            model=_model_name or DEFAULT_MODEL,
        )

    # Transcribe the entire ring with timestamps.
    try:
        result = _transcribe_window_with_words(state.audio_ring)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "v2 transcribe failed session=%s seq=%s", x_session_id, seq_i,
        )
        raise HTTPException(status_code=500, detail=f"transcribe failed: {exc}")

    words = result.get("words") or []
    # On is_final, finalize EVERYTHING — no draft.
    if is_final:
        finalized_candidates, draft_words = _split_words_into_finalized_draft(
            words, ring_duration, state.samples_consumed_before_ring,
        )
        finalized_candidates = finalized_candidates + draft_words
        draft_words = []
    else:
        finalized_candidates, draft_words = _split_words_into_finalized_draft(
            words, ring_duration, state.samples_consumed_before_ring,
        )

    # Determine which finalized_candidates have NOT yet been returned to
    # the client (i.e. are new since the last call). We do this by
    # comparing against state.finalized_words and only emitting the tail
    # that's new.
    already_finalized = state.finalized_words
    new_finalized: list[dict[str, Any]] = []
    overlap_len = min(len(already_finalized), len(finalized_candidates))
    # Walk forward looking for the first divergence; anything before is
    # already promoted, anything from there on is "new" finalized.
    first_new = overlap_len
    for i in range(overlap_len):
        if not _words_equal(already_finalized[i], finalized_candidates[i]):
            first_new = i
            break
    new_finalized = finalized_candidates[first_new:]

    # Update session state — the canonical finalized list is the union
    # of the previous and any newly stable words. We REPLACE the existing
    # state.finalized_words with finalized_candidates so any small wobble
    # in word boundaries gets picked up (NeMo timestamps can drift by a
    # few ms across calls as the ring evolves).
    state.finalized_words = finalized_candidates

    elapsed = time.time() - started
    rtf = elapsed / duration if duration > 0 else 0.0

    # Render convenience texts.
    text_finalized = " ".join(w["word"] for w in new_finalized if w.get("word"))
    text_draft = " ".join(w["word"] for w in draft_words if w.get("word"))

    # Log per-call. Useful for B.3 latency tracking.
    logger.info(
        "/transcribe-stream-v2 session=%s seq=%s ring=%.2fs duration=%.2fs "
        "elapsed=%.2fs rtf=%.3f new_final=%d draft=%d final=%s",
        x_session_id, seq_i, ring_duration, duration, elapsed, rtf,
        len(new_finalized), len(draft_words), is_final,
    )

    # On is_final, drop the session.
    if is_final:
        _sessions.pop(x_session_id, None)

    return TranscribeStreamV2Response(
        tokens_finalized=[StreamWordToken(**w) for w in new_finalized],
        tokens_draft=[StreamWordToken(**w) for w in draft_words],
        text_finalized=text_finalized,
        text_draft=text_draft,
        sequence=seq_i,
        is_final=is_final,
        session_id=x_session_id,
        ring_duration=ring_duration,
        session_audio_duration=session_audio_duration,
        elapsed_ms=elapsed * 1000.0,
        rtf=float(rtf),
        model=_model_name or DEFAULT_MODEL,
    )


@app.get("/stream-v2/sessions")
async def stream_v2_sessions() -> dict:
    """Debug-only: list active v2 sessions + their state shape. Useful
    for the B.3 spike to confirm session lifecycle works.
    """
    out = []
    now = time.time()
    for sid, st in _sessions.items():
        out.append({
            "session_id": sid,
            "first_seq": st.first_seq,
            "ring_seconds": len(st.audio_ring) / float(TARGET_SR),
            "finalized_word_count": len(st.finalized_words),
            "session_audio_seconds": (
                st.samples_consumed_before_ring + len(st.audio_ring)
            ) / float(TARGET_SR),
            "idle_seconds": now - st.last_seen,
        })
    return {"active_sessions": out, "count": len(out)}


@app.get("/")
async def root() -> dict:
    return {
        "service": "meet-parakeet-stream-svc",
        "version": SERVICE_VERSION,
        "phase": "B.3-spike",
        "model": _model_name or DEFAULT_MODEL,
        "model_loaded": _model is not None,
        "endpoints": {
            "v1_stateless": "/transcribe-stream",
            "v2_session_stateful": "/transcribe-stream-v2",
            "v2_session_debug":     "/stream-v2/sessions",
        },
        "docs": "/docs",
        "healthz": "/healthz",
        "design_doc": "docs/phase-b-server-live-streaming.md",
        "b3_spike_doc": "docs/phase-b3-nemo-streaming-spike.md",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8895, reload=False)
