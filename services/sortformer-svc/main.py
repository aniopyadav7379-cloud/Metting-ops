"""meet-sortformer-svc — Phase B.3 spike: NVIDIA Sortformer streaming
diarization microservice.

Why this exists
---------------
Today (v1.1.0), live partial transcripts from meet-parakeet-stream-svc
have **no speaker labels**. Diarization only happens post-hoc at session
finalize via meet-speaker-svc (pyannote community-1 + wespeaker against
the full session WAV). Users see "[unknown] said X" in the live view
and the canonical speaker names backfill ~60 s after the session ends.

NVIDIA's Sortformer is the design-doc-blessed live diarization path
(`docs/phase-b-server-live-streaming.md` section 5, "Choice 3"). It
emits stable speaker indices (0..3) on a streaming audio feed with
~0.3 s lookahead delay and DER notably better than pyannote on
LibriMix/CALLHOME benchmarks. The 4-speaker cap is fine for 90% of
the meetings we see (most calls have 1-3 talkers); the canonical
pyannote pass at session end handles >4 cases correctly and re-labels
if needed.

Endpoints
---------
GET  /health             — liveness + model-loaded flag + VRAM stats
GET  /readyz             — same shape, semantic alias
POST /diarize-stream     — accept WAV/PCM body, return speakers JSON.
                            Response shape:
                              { speakers: [{spk_id: 0,
                                             start_ms: 0,
                                             end_ms: 2560}, ...],
                                window_end_ms: 33500,
                                is_final: false,
                                model: "nvidia/diar_sortformer_4spk-v1",
                                duration_seconds: 33.5,
                                rtf: 0.027,
                                service_version: "0.1.0-spike" }
POST /diarize-file       — diarize a file by absolute path on the
                            container (for shared-volume callers, e.g.
                            the finalize-audio worker). Same response
                            shape as /diarize-stream but no streaming
                            semantics.
POST /diarize-file-upload — v2.2.0 canonical-hybrid path. Multipart WAV
                            upload (cross-host, no shared mount needed).
                            Sortformer segmentation + per-turn wespeaker
                            embeddings from meet-speaker-svc. Returns a
                            pyannote-compatible DiarizeResponse so the
                            backend provider drops in for the pyannote one.

Model + streaming notes
-----------------------
We use `nvidia/diar_sortformer_4spk-v1` because:
  - The Phase B.2 parakeet-stream-svc container we layer on ships
    NeMo 2.4.1. v1 loads + diarizes cleanly on this NeMo.
  - The newer streaming v2 / v2.1 models reference a
    `spkcache_update_period` arg the 2.4.1 SortformerModules
    constructor does not accept (verified in the spike — see
    docs/phase-b3-sortformer-spike.md). Upgrading NeMo is a separate
    deploy decision (the batch parakeet-svc pinned 2.4.1 for stability
    reasons; we don't want to bump NeMo under it without a regression
    pass), so we ship v1 for the spike and call the v2.x upgrade out
    in the spike report.

v1's `forward_streaming_step()` API supports true incremental
streaming (FIFO + speaker-cache state across chunks), but the
spike's POST surface uses the simpler one-shot `diarize()` API
on a rolling caller-managed window. This keeps the wire contract
stateless (each POST is independent) which:
  - matches the existing meet-parakeet-stream-svc contract one-for-one,
  - removes the need for sticky routing or Redis pub/sub for session
    state coordination,
  - lets us prototype + measure on a real GPU without committing to a
    streaming-state design we'd want to revisit when we move to v2.x.

The follow-up promotion path (called out in the spike report) is to
add a `/diarize-stream/append` endpoint that holds a
`SortformerStreamingState` per `X-Session-Id`, calls
`forward_streaming_step()` per window, and emits only the *new*
speaker labels since the last call. That's strictly a same-process
state change with the same HF model — no Docker/compose changes.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import httpx
import numpy as np
import soundfile as sf
import torch
import torchaudio
from fastapi import (
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from pydantic import BaseModel, Field

logger = logging.getLogger("sortformer-svc")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
TARGET_SR = 16000  # Sortformer v1 is trained at 16 kHz mono

# v1 is the canonical spike model. v2 / v2.1 require NeMo >= 2.5 — see
# spike report and module docstring above. Env override exists so a
# future NeMo bump can flip to v2.1 without changing this file.
DEFAULT_MODEL = os.getenv(
    "SORTFORMER_MODEL", "nvidia/diar_sortformer_4spk-v1"
)
# No fallback list today — v1 either loads on this NeMo or it doesn't,
# and there's no compatible older Sortformer to fall back to. Kept the
# env knob in case we add a custom-finetuned variant.
FALLBACK_MODEL = os.getenv("SORTFORMER_FALLBACK_MODEL", "").strip() or None
MODEL_PRECISION = os.getenv("SORTFORMER_PRECISION", "fp16").strip().lower()

# v1 is hard-capped at 4 speakers by the model architecture (4 output
# heads). Live attribution beyond 4 is impossible; the canonical
# pyannote pass at session end re-labels correctly when speaker count
# > 4. The hard cap is a Sortformer property, not a service config.
MAX_SPEAKERS = 4

# Window sizing safety rails. v1's diarize() handles long audio fine
# (it chunks internally), but for the streaming surface we cap at 60 s
# per POST so a misbehaving client can't pin the GPU on a 10 min clip.
# The /diarize-file endpoint allows longer audio because it's the
# finalize path. Minimum is 0.5 s — Sortformer's first encoder frame
# needs ~0.08 s of audio to produce, anything shorter degenerates.
MIN_AUDIO_SECONDS = float(os.getenv("SORTFORMER_MIN_AUDIO_SECONDS", "0.5"))
MAX_STREAM_AUDIO_SECONDS = float(
    os.getenv("SORTFORMER_MAX_STREAM_AUDIO_SECONDS", "60.0")
)
MAX_FILE_AUDIO_SECONDS = float(
    os.getenv("SORTFORMER_MAX_FILE_AUDIO_SECONDS", "14400.0")  # 4 h
)

# Batch size is 1 by design — the streaming surface is one window per
# call. Bump only if we add a true batch endpoint. Env knob so the
# spike's leak_bench.py can sweep it without code changes.
BATCH_SIZE = int(os.getenv("SORTFORMER_BATCH_SIZE", "1"))
_gpu_semaphore = asyncio.Semaphore(max(1, int(os.getenv("MAX_CONCURRENT_DIARIZATIONS", "1"))))


@asynccontextmanager
async def _gpu_slot():
    try:
        await asyncio.wait_for(_gpu_semaphore.acquire(), timeout=0.01)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="GPU is at capacity; retry shortly",
            headers={"Retry-After": "5"},
        ) from exc
    try:
        yield
    finally:
        _gpu_semaphore.release()

# v2.2.0 canonical-hybrid path: /diarize-file-upload diarizes with
# Sortformer then asks meet-speaker-svc (wespeaker) for a per-turn voice
# embedding so the backend's identify_speakers can match enrolled voices
# — exactly what pyannote's /diarize gives today, but with Sortformer
# doing the speaker segmentation. speaker-svc is a sibling container on
# the same midboy2 host, so this call stays on the local network. The
# Tailscale IP (http://meet-speaker-svc:8889) is the documented sibling
# address and is the safe default; override with SPEAKER_SVC_URL to use
# the docker-network name on hosts where that resolves.
SPEAKER_SVC_URL = os.getenv(
    "SPEAKER_SVC_URL", "http://meet-speaker-svc:8889"
).rstrip("/")
SPEAKER_SVC_TIMEOUT = float(os.getenv("SPEAKER_SVC_TIMEOUT", "60"))
# Turns shorter than this are not sent to speaker-svc /embed (which
# rejects clips < 0.5 s) — they get embedding=None, matching how
# speaker-svc itself skips embedding short turns. Keep at/above
# speaker-svc's MIN_AUDIO_SECONDS.
EMBED_MIN_SECONDS = float(os.getenv("SORTFORMER_EMBED_MIN_SECONDS", "0.5"))

SERVICE_VERSION = "0.1.0-spike"  # Phase B.3 spike — research + prototype

# Module-level model state — single instance, kept warm across calls.
_model: Any = None
_model_name: str = ""
_model_load_error: Optional[str] = None
_model_load_seconds: Optional[float] = None
_request_count: int = 0


def _load_model() -> Any:
    """Eager-load Sortformer at startup. Tries DEFAULT_MODEL first,
    falls back to FALLBACK_MODEL on any exception. Idempotent — cheap
    to call repeatedly once warm.
    """
    global _model, _model_name, _model_load_error, _model_load_seconds
    if _model is not None:
        return _model

    # Deferred import — keeps module-level import cheap so /health can
    # respond even if NeMo is broken in this image (degraded mode).
    from nemo.collections.asr.models import SortformerEncLabelModel

    candidates = [DEFAULT_MODEL]
    if FALLBACK_MODEL and FALLBACK_MODEL != DEFAULT_MODEL:
        candidates.append(FALLBACK_MODEL)

    last_exc: Optional[Exception] = None
    for name in candidates:
        try:
            logger.info("Loading Sortformer %s on %s", name, DEVICE)
            t0 = time.time()
            model = SortformerEncLabelModel.from_pretrained(model_name=name)
            model = model.to(DEVICE)
            if MODEL_PRECISION in ("fp16", "half", "float16"):
                if DEVICE == "cuda":
                    model = model.half()
                    logger.info("Sortformer precision = fp16")
                else:
                    logger.warning("Ignoring fp16 precision on non-CUDA")
            try:
                model.eval()
            except Exception:
                pass
            _model = model
            _model_name = name
            _model_load_error = None
            _model_load_seconds = time.time() - t0
            logger.info(
                "Sortformer ready: %s (load took %.1f s, %.0f M params)",
                name, _model_load_seconds,
                sum(p.numel() for p in model.parameters()) / 1e6,
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

    msg = f"All Sortformer candidates failed; last error: {last_exc}"
    logger.error(msg)
    _model_load_error = str(last_exc) if last_exc else "unknown error"
    raise RuntimeError(msg)


def _decode_audio(buf: bytes) -> tuple[np.ndarray, float]:
    """Accept raw bytes — preferred is WAV (what the WS handler wraps
    PCM16 in). Falls back to treating the input as raw PCM16 LE at
    16 kHz mono if WAV parsing fails (a forgiving streaming-path
    behaviour matching parakeet-stream-svc).

    Returns (mono float32 waveform at TARGET_SR, duration seconds).
    """
    wav: Optional[np.ndarray] = None
    sr: int = 0

    # Try WAV first.
    try:
        wav, sr = sf.read(io.BytesIO(buf), dtype="float32", always_2d=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("WAV parse failed (%s); trying raw PCM16", exc)

    # Raw PCM16 fallback.
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


def _parse_segment_str(seg_str: str) -> Optional[dict[str, Any]]:
    """Parse Sortformer's segment string format:
        "0.000 2.560 speaker_0"
    into a dict. Returns None if the string is malformed.
    """
    parts = seg_str.strip().split()
    if len(parts) < 3:
        return None
    try:
        start = float(parts[0])
        end = float(parts[1])
    except ValueError:
        return None
    spk_label = " ".join(parts[2:]).strip()
    # Extract trailing integer from "speaker_0" / "spk_0" / "0".
    spk_id: int
    if "_" in spk_label:
        tail = spk_label.split("_")[-1]
    else:
        tail = spk_label
    try:
        spk_id = int(tail)
    except ValueError:
        # Unknown label format — pass through hash-mod-MAX_SPEAKERS so
        # the response still has an int spk_id field.
        spk_id = abs(hash(spk_label)) % MAX_SPEAKERS
    return {
        "spk_id": spk_id,
        "spk_label": spk_label,
        "start_ms": int(round(start * 1000)),
        "end_ms": int(round(end * 1000)),
    }


def _diarize_path(path: str) -> dict[str, Any]:
    """Run Sortformer's one-shot diarize() against a path on disk.
    Returns the parsed JSON-friendly result dict (segments + timing).
    """
    model = _load_model()

    t0 = time.time()
    # diarize() yields one entry per audio in the input list — we pass a
    # single path so we read [0].
    try:
        with torch.inference_mode():
            results = model.diarize(
                audio=[path],
                batch_size=BATCH_SIZE,
                include_tensor_outputs=False,
                verbose=False,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("sortformer diarize failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"diarize failed: {exc}")
    elapsed = time.time() - t0

    if not results:
        return {"speakers": [], "elapsed": elapsed, "model": _model_name}

    raw_segments = results[0]
    speakers: list[dict[str, Any]] = []
    if isinstance(raw_segments, (list, tuple)):
        for seg in raw_segments:
            if isinstance(seg, str):
                parsed = _parse_segment_str(seg)
                if parsed is not None:
                    speakers.append(parsed)
            elif isinstance(seg, (list, tuple)) and len(seg) >= 3:
                # [start, end, label] tuple form some NeMo versions return.
                start_ms = int(round(float(seg[0]) * 1000))
                end_ms = int(round(float(seg[1]) * 1000))
                label = str(seg[2])
                parsed = _parse_segment_str(f"{seg[0]} {seg[1]} {label}")
                if parsed is None:
                    parsed = {
                        "spk_id": abs(hash(label)) % MAX_SPEAKERS,
                        "spk_label": label,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                    }
                speakers.append(parsed)
            else:
                logger.debug("skip unknown segment shape: %r", seg)

    # Sortformer ordering is not guaranteed; sort by start_ms for caller
    # convenience.
    speakers.sort(key=lambda s: s["start_ms"])
    return {
        "speakers": speakers,
        "elapsed": elapsed,
        "model": _model_name,
    }


def _diarize_wav_array(wav: np.ndarray) -> dict[str, Any]:
    """Write the waveform to a tempfile WAV and run _diarize_path().
    NeMo's diarize() takes file paths today (no in-memory tensor
    overload), so the tempfile detour is the cheapest interface.
    PCM16 write is ~1 ms for our 60 s cap windows."""
    fd, path = tempfile.mkstemp(prefix="sortformer_", suffix=".wav")
    os.close(fd)
    try:
        sf.write(path, wav, TARGET_SR, subtype="PCM_16")
        return _diarize_path(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _embed_turns_via_speaker_svc(
    wav: np.ndarray, turns: list[dict[str, Any]]
) -> list[Optional[list[float]]]:
    """Crop each diarized turn out of `wav` and ask meet-speaker-svc for a
    wespeaker embedding.

    Returns a list parallel to `turns`: each entry is the embedding float
    list, or None when the turn is too short to embed or speaker-svc could
    not be reached. Embeddings are best-effort enrichment for the backend's
    identify_speakers step — the speaker *labels* from Sortformer are the
    primary product, so this never raises. If speaker-svc is unreachable we
    stop after the first connection error rather than eating one timeout per
    turn.
    """
    embeddings: list[Optional[list[float]]] = [None] * len(turns)
    if not turns:
        return embeddings

    min_samples = int(EMBED_MIN_SECONDS * TARGET_SR)
    embedded = 0
    async with httpx.AsyncClient(timeout=SPEAKER_SVC_TIMEOUT) as client:
        for i, turn in enumerate(turns):
            start_idx = max(0, int(turn["start_ms"] / 1000.0 * TARGET_SR))
            end_idx = min(len(wav), int(turn["end_ms"] / 1000.0 * TARGET_SR))
            if end_idx - start_idx < min_samples:
                continue  # too short for speaker-svc — leave as None
            buf = io.BytesIO()
            sf.write(
                buf, wav[start_idx:end_idx], TARGET_SR,
                subtype="PCM_16", format="WAV",
            )
            try:
                resp = await client.post(
                    f"{SPEAKER_SVC_URL}/embed",
                    files={"audio": ("turn.wav", buf.getvalue(), "audio/wav")},
                )
                resp.raise_for_status()
                emb = resp.json().get("embedding") or None
                if emb:
                    embeddings[i] = emb
                    embedded += 1
            except httpx.HTTPStatusError as exc:
                # Per-clip rejection (e.g. clip just under the threshold) —
                # skip this turn only, keep going.
                logger.warning(
                    "speaker-svc /embed HTTP %s turn %d: %s",
                    exc.response.status_code, i, exc.response.text[:150],
                )
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                logger.warning(
                    "speaker-svc unreachable at %s (%s); skipping embeddings "
                    "for remaining %d turns",
                    SPEAKER_SVC_URL, exc, len(turns) - i,
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("speaker-svc /embed failed turn %d: %s", i, exc)
    logger.info(
        "embedded %d/%d turns via speaker-svc (%s)",
        embedded, len(turns), SPEAKER_SVC_URL,
    )
    return embeddings


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
    gpu_name: Optional[str] = None
    precision: str = MODEL_PRECISION
    max_speakers: int = MAX_SPEAKERS
    vram_alloc_mib: Optional[int] = None
    vram_reserved_mib: Optional[int] = None
    request_count: int = 0
    phase: str = "B.3-spike"


class SpeakerSegment(BaseModel):
    spk_id: int = Field(..., ge=0, le=MAX_SPEAKERS - 1)
    spk_label: str
    start_ms: int = Field(..., ge=0)
    end_ms: int = Field(..., ge=0)


class DiarizeStreamResponse(BaseModel):
    speakers: list[SpeakerSegment] = Field(default_factory=list)
    window_end_ms: int = Field(..., ge=0)
    is_final: bool = False
    model: str
    duration_seconds: float
    rtf: float
    distinct_speaker_count: int = 0
    sequence: Optional[int] = None
    service_version: str = SERVICE_VERSION


class DiarizeFileSegment(BaseModel):
    """Pyannote-compatible turn: seconds-based start/end, a SPEAKER_NN
    label, and an optional wespeaker embedding. Mirrors
    meet-speaker-svc's DiarizeSegment so SortformerSpeakerSvcProvider
    parses this response exactly like LocalSpeakerSvcProvider parses
    speaker-svc's /diarize."""
    start: float
    end: float
    speaker: str
    embedding: Optional[list[float]] = None


class DiarizeFileResponse(BaseModel):
    """Mirror of meet-speaker-svc's DiarizeResponse, plus model/rtf so the
    finalize pipeline can log throughput. backend is fixed to
    'sortformer-hybrid' to make the source obvious in stored transcripts."""
    segments: list[DiarizeFileSegment] = Field(default_factory=list)
    num_speakers: int = 0
    backend: str = "sortformer-hybrid"
    duration_seconds: float = 0.0
    model: str
    rtf: float = 0.0
    embeddings_enabled: bool = True
    service_version: str = SERVICE_VERSION


# ---------- lifespan + app ----------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Warm the model at startup so the first /diarize-stream isn't
    gated on ~2-3 s of NeMo init. If load fails we still come up so
    /health can report the error to the operator (the alternative is
    crash-looping which makes diagnosis harder)."""
    try:
        _load_model()
    except Exception as exc:  # noqa: BLE001
        logger.error("Startup model load failed: %s", exc)
    yield


app = FastAPI(
    title="meet-sortformer-svc",
    version=SERVICE_VERSION,
    description=(
        "Phase B.3 spike: NVIDIA Sortformer (v1, 4-speaker) live "
        "diarization. Counterpart to meet-speaker-svc (post-hoc) and "
        "meet-parakeet-stream-svc (live ASR)."
    ),
    lifespan=lifespan,
)


# ---------- routes ----------


def _vram_stats() -> tuple[Optional[int], Optional[int]]:
    if not torch.cuda.is_available():
        return None, None
    try:
        return (
            int(torch.cuda.memory_allocated() / 1024**2),
            int(torch.cuda.memory_reserved() / 1024**2),
        )
    except Exception:
        return None, None


def _gpu_name() -> Optional[str]:
    if not torch.cuda.is_available():
        return None
    try:
        return torch.cuda.get_device_name(0)
    except Exception:
        return None


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    vram_alloc, vram_reserved = _vram_stats()
    cuda = torch.cuda.is_available()
    status = (
        "ok" if _model is not None
        else ("loading" if _model_load_error is None else "degraded")
    )
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
        gpu_name=_gpu_name(),
        precision=MODEL_PRECISION,
        max_speakers=MAX_SPEAKERS,
        vram_alloc_mib=vram_alloc,
        vram_reserved_mib=vram_reserved,
        request_count=_request_count,
        phase="B.3-spike",
    )


@app.get("/readyz", response_model=HealthResponse)
def readyz() -> HealthResponse:
    """Same body as /health; convention alias for compose healthchecks
    that prefer /readyz semantics."""
    return health()


@app.post("/diarize-stream", response_model=DiarizeStreamResponse)
async def diarize_stream(
    request: Request,
    x_session_id: Optional[str] = Header(default=None, alias="X-Session-Id"),
    x_flush_sequence: Optional[str] = Header(default=None, alias="X-Flush-Sequence"),
    x_is_final: Optional[str] = Header(default=None, alias="X-Is-Final"),
    x_window_start_ms: Optional[str] = Header(default=None, alias="X-Window-Start-Ms"),
) -> DiarizeStreamResponse:
    """Diarize a single short audio window.

    Request body: WAV bytes (preferred — clients wrap PCM16 in a
    minimal RIFF header) or raw PCM16 LE at 16 kHz mono.

    Headers (all optional):
      X-Session-Id        — opaque per-WS session identifier for log corr.
      X-Flush-Sequence    — per-session monotonic counter from caller.
      X-Is-Final          — "1" if this is the last window of the segment.
      X-Window-Start-Ms   — absolute ms-since-session-start of this
                             window. If provided, all start_ms / end_ms
                             in the response are offset by this value so
                             the caller can stitch absolute timeline
                             coordinates without bookkeeping.

    Response: speakers list (one entry per turn) + window_end_ms (the
    end of this window in caller-absolute coordinates if X-Window-Start-Ms
    was provided, else window-relative), is_final flag echoed back from
    the request, the actual model name used, and the realtime factor
    for this window.
    """
    global _request_count
    started = time.time()

    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio body")

    try:
        seq_i: Optional[int] = (
            int(x_flush_sequence) if x_flush_sequence else None
        )
    except ValueError:
        seq_i = None
    is_final = (x_is_final == "1")

    try:
        window_offset_ms = (
            int(x_window_start_ms) if x_window_start_ms else 0
        )
    except ValueError:
        window_offset_ms = 0

    wav, duration = _decode_audio(raw)
    if duration < MIN_AUDIO_SECONDS:
        # Empty / sub-frame buffer — return an empty response rather
        # than 400 so the WS handler's de-dup logic stays simple.
        return DiarizeStreamResponse(
            speakers=[],
            window_end_ms=window_offset_ms,
            is_final=is_final,
            model=_model_name or DEFAULT_MODEL,
            duration_seconds=duration,
            rtf=0.0,
            distinct_speaker_count=0,
            sequence=seq_i,
        )
    if duration > MAX_STREAM_AUDIO_SECONDS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"window too long ({duration:.1f}s > "
                f"{MAX_STREAM_AUDIO_SECONDS:.0f}s) — use /diarize-file "
                "for full meeting audio"
            ),
        )

    if _model is None and _model_load_error is None:
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
        async with _gpu_slot():
            result = await asyncio.to_thread(_diarize_wav_array, wav)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "diarize-stream failed session=%s seq=%s", x_session_id, seq_i,
        )
        raise HTTPException(
            status_code=500, detail=f"diarize failed: {exc}",
        )

    elapsed = time.time() - started
    rtf = elapsed / duration if duration > 0 else 0.0

    speakers_raw = result.get("speakers") or []
    # Offset everything by window_offset_ms if the caller asked.
    speakers: list[SpeakerSegment] = []
    distinct: set[int] = set()
    for s in speakers_raw:
        spk = SpeakerSegment(
            spk_id=int(s["spk_id"]),
            spk_label=str(s["spk_label"]),
            start_ms=int(s["start_ms"]) + window_offset_ms,
            end_ms=int(s["end_ms"]) + window_offset_ms,
        )
        speakers.append(spk)
        distinct.add(spk.spk_id)

    window_end_ms = window_offset_ms + int(round(duration * 1000))

    _request_count += 1
    logger.info(
        "/diarize-stream session=%s seq=%s duration=%.2fs elapsed=%.2fs "
        "rtf=%.3f speakers=%d distinct=%d final=%s",
        x_session_id, seq_i, duration, elapsed, rtf,
        len(speakers), len(distinct), is_final,
    )

    return DiarizeStreamResponse(
        speakers=speakers,
        window_end_ms=window_end_ms,
        is_final=is_final,
        model=_model_name or DEFAULT_MODEL,
        duration_seconds=duration,
        rtf=rtf,
        distinct_speaker_count=len(distinct),
        sequence=seq_i,
    )


@app.post("/diarize-file", response_model=DiarizeStreamResponse)
async def diarize_file(
    audio_path: str = Query(..., description="Absolute path on container"),
    x_session_id: Optional[str] = Header(default=None, alias="X-Session-Id"),
) -> DiarizeStreamResponse:
    """Diarize an audio file by absolute path on the container.

    Caller is responsible for ensuring the path is reachable from this
    container (typically via a shared bind-mount). This endpoint exists
    for the finalize-audio worker which has access to the session WAV
    on the bigboy filesystem; the meet-backend reaches it via the
    sortformer container's bind-mounted /srv/uploads volume.

    No streaming semantics — single shot, may take seconds for long
    audio. Use /diarize-stream for live windows.
    """
    global _request_count
    started = time.time()

    if not audio_path:
        raise HTTPException(status_code=400, detail="audio_path required")
    if not os.path.exists(audio_path):
        raise HTTPException(
            status_code=400,
            detail=f"audio_path not found in container: {audio_path}",
        )

    # Probe duration to enforce the upper bound before we hand to NeMo.
    try:
        info = sf.info(audio_path)
        duration = float(info.frames) / float(info.samplerate)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"cannot read audio info: {exc}",
        )
    if duration > MAX_FILE_AUDIO_SECONDS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"file too long ({duration:.1f}s > "
                f"{MAX_FILE_AUDIO_SECONDS:.0f}s)"
            ),
        )
    if duration < MIN_AUDIO_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"file too short ({duration:.2f}s)",
        )

    if _model is None and _model_load_error is None:
        raise HTTPException(
            status_code=503, detail="model still loading — retry shortly",
        )
    if _model is None:
        raise HTTPException(
            status_code=503,
            detail=f"model load failed: {_model_load_error}",
        )

    try:
        async with _gpu_slot():
            result = await asyncio.to_thread(_diarize_path, audio_path)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("diarize-file failed session=%s", x_session_id)
        raise HTTPException(
            status_code=500, detail=f"diarize failed: {exc}",
        )

    elapsed = time.time() - started
    rtf = elapsed / duration if duration > 0 else 0.0

    speakers_raw = result.get("speakers") or []
    speakers: list[SpeakerSegment] = []
    distinct: set[int] = set()
    for s in speakers_raw:
        spk = SpeakerSegment(
            spk_id=int(s["spk_id"]),
            spk_label=str(s["spk_label"]),
            start_ms=int(s["start_ms"]),
            end_ms=int(s["end_ms"]),
        )
        speakers.append(spk)
        distinct.add(spk.spk_id)

    _request_count += 1
    logger.info(
        "/diarize-file session=%s path=%s duration=%.1fs elapsed=%.1fs "
        "rtf=%.3f speakers=%d distinct=%d",
        x_session_id, audio_path, duration, elapsed, rtf,
        len(speakers), len(distinct),
    )

    return DiarizeStreamResponse(
        speakers=speakers,
        window_end_ms=int(round(duration * 1000)),
        is_final=True,
        model=_model_name or DEFAULT_MODEL,
        duration_seconds=duration,
        rtf=rtf,
        distinct_speaker_count=len(distinct),
        sequence=None,
    )


@app.post("/diarize-file-upload", response_model=DiarizeFileResponse)
async def diarize_file_upload(
    audio: UploadFile = File(...),
    return_embeddings: bool = Form(default=True),
    x_session_id: Optional[str] = Header(default=None, alias="X-Session-Id"),
) -> DiarizeFileResponse:
    """Canonical post-hoc diarization for the finalize-audio pipeline.

    Multipart counterpart to /diarize-file. The caller streams the session
    WAV in the request body rather than passing a path, because the backend
    (bigboy) and this service (midboy2) do not share a filesystem — the
    same reason meet-speaker-svc's provider always uploads bytes. We run
    Sortformer for speaker segmentation, then ask meet-speaker-svc
    (wespeaker) for a per-turn voice embedding, and return a
    pyannote-compatible DiarizeResponse so the backend's
    SortformerSpeakerSvcProvider is a drop-in for LocalSpeakerSvcProvider.

    Sortformer caps at 4 speakers; the canonical pyannote path (default)
    handles >4. Set return_embeddings=false to skip the speaker-svc round
    trips when you only need labels.
    """
    global _request_count
    started = time.time()

    raw = await audio.read()
    if not raw:
        raise HTTPException(status_code=400, detail="empty audio upload")

    wav, duration = _decode_audio(raw)
    if duration < MIN_AUDIO_SECONDS:
        raise HTTPException(
            status_code=400, detail=f"audio too short ({duration:.2f}s)",
        )
    if duration > MAX_FILE_AUDIO_SECONDS:
        raise HTTPException(
            status_code=413,
            detail=(
                f"file too long ({duration:.1f}s > "
                f"{MAX_FILE_AUDIO_SECONDS:.0f}s)"
            ),
        )

    if _model is None and _model_load_error is None:
        raise HTTPException(
            status_code=503, detail="model still loading — retry shortly",
        )
    if _model is None:
        raise HTTPException(
            status_code=503, detail=f"model load failed: {_model_load_error}",
        )

    try:
        async with _gpu_slot():
            result = await asyncio.to_thread(_diarize_wav_array, wav)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("diarize-file-upload failed session=%s", x_session_id)
        raise HTTPException(status_code=500, detail=f"diarize failed: {exc}")

    turns = result.get("speakers") or []

    embeddings: list[Optional[list[float]]] = [None] * len(turns)
    if return_embeddings and turns:
        embeddings = await _embed_turns_via_speaker_svc(wav, turns)

    segments: list[DiarizeFileSegment] = []
    distinct: set[int] = set()
    for i, turn in enumerate(turns):
        spk_id = int(turn["spk_id"])
        distinct.add(spk_id)
        segments.append(DiarizeFileSegment(
            start=turn["start_ms"] / 1000.0,
            end=turn["end_ms"] / 1000.0,
            # Match pyannote's SPEAKER_NN convention so identify_speakers
            # and the UI treat hybrid labels identically to pyannote's.
            speaker=f"SPEAKER_{spk_id:02d}",
            embedding=embeddings[i],
        ))

    elapsed = time.time() - started
    rtf = elapsed / duration if duration > 0 else 0.0

    _request_count += 1
    logger.info(
        "/diarize-file-upload session=%s duration=%.1fs elapsed=%.1fs "
        "rtf=%.3f turns=%d speakers=%d embeddings=%s",
        x_session_id, duration, elapsed, rtf,
        len(segments), len(distinct), return_embeddings,
    )

    return DiarizeFileResponse(
        segments=segments,
        num_speakers=len(distinct),
        backend="sortformer-hybrid",
        duration_seconds=duration,
        model=_model_name or DEFAULT_MODEL,
        rtf=rtf,
        embeddings_enabled=return_embeddings,
    )


@app.get("/")
async def root() -> dict:
    return {
        "service": "meet-sortformer-svc",
        "version": SERVICE_VERSION,
        "phase": "B.3-spike",
        "model": _model_name or DEFAULT_MODEL,
        "model_loaded": _model is not None,
        "max_speakers": MAX_SPEAKERS,
        "docs": "/docs",
        "health": "/health",
        "design_doc": "docs/phase-b-server-live-streaming.md",
        "spike_report": "docs/phase-b3-sortformer-spike.md",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8896, reload=False)
