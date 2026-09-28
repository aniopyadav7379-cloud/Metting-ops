"""speaker-svc — FastAPI wrapper around pyannote wespeaker + diarization.

Endpoints
---------
GET  /health                — liveness, model status, GPU info
GET  /healthz/synthetic     — full-pipeline probe: runs bundled 2-speaker WAV
                              through pyannote + wespeaker, asserts num_speakers == 2.
                              Returns 200 OK on pass, 503 on degraded.
POST /embed                 — extract a 256-d wespeaker embedding from a clip
POST /diarize               — diarize an audio file (Pyannote if token, else whole-clip fallback)
POST /identify              — match a clip against a list of known embeddings (cosine sim)

The container runs inside the meet-internal Docker network on bigboy GPU 1.
Audio is sent as a file path (when caller and svc share a mount) or as bytes
in the multipart body (always supported).
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch
import torchaudio
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("speaker-svc")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
EMBEDDER_DEVICE = os.getenv("SPEAKER_EMBEDDER_DEVICE", "cpu").strip().lower() or "cpu"
DIARIZER_DEVICE = os.getenv("PYANNOTE_DIARIZER_DEVICE", "cpu").strip().lower() or "cpu"
EAGER_LOAD_EMBEDDER = os.getenv("SPEAKER_EAGER_LOAD_EMBEDDER", "false").lower() in ("1", "true", "yes")
EAGER_LOAD_DIARIZER = os.getenv("SPEAKER_EAGER_LOAD_DIARIZER", "false").lower() in ("1", "true", "yes")
TARGET_SR = 16000  # wespeaker + Pyannote both expect 16 kHz mono
MIN_SECONDS = float(os.getenv("MIN_AUDIO_SECONDS", "0.5"))
MAX_SECONDS = float(os.getenv("MAX_AUDIO_SECONDS", "1800"))  # 30 min
# Long-audio chunked diarization. When a clip exceeds CHUNK_THRESHOLD_SECONDS we
# split it into CHUNK_WINDOW_SECONDS windows, diarize each window single-pass on
# the GPU (bounded peak memory + time per window), then STITCH each window's
# local SPEAKER_xx clusters into one global set by wespeaker-embedding cosine so
# the same person keeps one label across windows. Mirrors how parakeet-svc
# chunks long audio for STT. At/under the threshold we stay single-pass (pyannote
# clusters globally = most accurate). MAX_SECONDS stays the absolute ceiling.
CHUNK_THRESHOLD_SECONDS = float(os.getenv("CHUNK_THRESHOLD_SECONDS", "28800"))  # 8h: below = single-pass
CHUNK_WINDOW_SECONDS = float(os.getenv("CHUNK_WINDOW_SECONDS", "3600"))         # 60 min
# Cross-window centroid cosine to call two windows the same speaker. Empirically
# (acbe7b3a, 6 windows) major speakers stitch at 0.73-0.96 and distinct speakers
# at ~0.0; 0.50 sits cleanly between. NOTE: single-pass (<= threshold) is more
# accurate for minor/ambiguous speakers — chunking is the >8h safety net, where
# pyannote's O(n^2) global clustering would otherwise choke.
CHUNK_STITCH_THRESHOLD = float(os.getenv("CHUNK_STITCH_THRESHOLD", "0.50"))
HF_TOKEN = os.getenv("HUGGINGFACE_TOKEN", "").strip() or None
# Task #79 v2: call torch.cuda.empty_cache() after every Nth /diarize. Set to
# 0 to disable. Default 1 (every request) — measure throughput impact and
# bump to 5/10 if material. The caching allocator legitimately holds memory
# across calls; periodic clearing reduces peak reservation in a long-running
# service and is the primary leak-mitigation knob.
EMPTY_CACHE_EVERY = int(os.getenv("SPEAKER_EMPTY_CACHE_EVERY", "1"))
EMBEDDING_MODEL = "pyannote/wespeaker-voxceleb-resnet34-LM"
DIARIZATION_MODEL = "pyannote/speaker-diarization-community-1"
DIARIZATION_FALLBACK_MODEL = "pyannote/speaker-diarization-3.1"

app = FastAPI(title="speaker-svc", version="0.1.0")

_embedder = None
_diarizer = None
_diarizer_backend: Optional[str] = None
_diarizer_load_error: Optional[str] = None
# Task #79 v2: /diarize request counter, used to gate periodic empty_cache().
_diarize_request_count: int = 0
_gpu_semaphore = asyncio.Semaphore(max(1, int(os.getenv("MAX_CONCURRENT_DIARIZATIONS", "1"))))
GPU_QUEUE_TIMEOUT = float(os.getenv("SPEAKER_GPU_QUEUE_TIMEOUT", "600"))


@asynccontextmanager
async def _gpu_slot():
    try:
        # Internal callers may launch several meetings concurrently while this
        # service intentionally runs one GPU job at a time.  Queue them here
        # instead of immediately returning 503 and silently dropping speaker
        # data.  The event loop remains free while requests wait.
        await asyncio.wait_for(
            _gpu_semaphore.acquire(),
            timeout=max(0.01, GPU_QUEUE_TIMEOUT),
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="GPU queue wait timed out; retry shortly",
            headers={"Retry-After": "5"},
        ) from exc
    try:
        yield
    finally:
        _gpu_semaphore.release()


def _load_embedder():
    """Lazy-load the wespeaker embedder. Cached after first call."""
    global _embedder
    if _embedder is not None:
        return _embedder
    if not HF_TOKEN:
        raise RuntimeError(f"HUGGINGFACE_TOKEN not set; {EMBEDDING_MODEL} unavailable")
    from pyannote.audio import Inference, Model
    logger.info("Loading %s on %s", EMBEDDING_MODEL, EMBEDDER_DEVICE)
    try:
        embedding_model = Model.from_pretrained(EMBEDDING_MODEL, token=HF_TOKEN)
    except TypeError:
        embedding_model = Model.from_pretrained(EMBEDDING_MODEL, use_auth_token=HF_TOKEN)
    _embedder = Inference(embedding_model, window="whole")
    if EMBEDDER_DEVICE == "cuda":
        _embedder = _embedder.to(torch.device("cuda"))
        torch.cuda.empty_cache()
    return _embedder


def _load_diarizer():
    """Lazy-load Pyannote community-1 with a 3.1 fallback."""
    global _diarizer, _diarizer_backend, _diarizer_load_error
    if _diarizer is not None:
        return _diarizer
    if not HF_TOKEN:
        _diarizer_load_error = f"HUGGINGFACE_TOKEN not set; {DIARIZATION_MODEL} unavailable"
        return None
    try:
        from pyannote.audio import Pipeline
        loaded_model = DIARIZATION_MODEL
        logger.info("Loading %s on %s", DIARIZATION_MODEL, DIARIZER_DEVICE)
        try:
            try:
                pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, token=HF_TOKEN)
            except TypeError:
                pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL, use_auth_token=HF_TOKEN)
            _diarizer_backend = "pyannote-community-1"
        except Exception as community_exc:  # noqa: BLE001
            logger.warning(
                "%s load failed, falling back to %s: %s",
                DIARIZATION_MODEL,
                DIARIZATION_FALLBACK_MODEL,
                community_exc,
            )
            loaded_model = DIARIZATION_FALLBACK_MODEL
            try:
                pipeline = Pipeline.from_pretrained(DIARIZATION_FALLBACK_MODEL, token=HF_TOKEN)
            except TypeError:
                pipeline = Pipeline.from_pretrained(DIARIZATION_FALLBACK_MODEL, use_auth_token=HF_TOKEN)
            _diarizer_backend = "pyannote-3.1"
        if DIARIZER_DEVICE == "cuda":
            pipeline.to(torch.device("cuda"))
            torch.cuda.empty_cache()
        # Tune clustering + segmentation. Threshold has two failure modes:
        # - too low (e.g. 0.65 from Meeting Minutes): over-splits one
        #   speaker's varied speech into multiple clusters. Aaron's
        #   5-speaker Legacy 21 came back as 14 at 0.65.
        # - too high (~0.7045 model default): merges similar voices into
        #   one. The Citadel meeting came back as 1 speaker at default
        #   before this knob existed.
        # 0.72 sits a hair above the model default, biasing toward
        # merging when in doubt. Env-tunable so we can dial per deploy
        # without rebuilding.
        clustering_thr = float(os.getenv("PYANNOTE_CLUSTERING_THRESHOLD", "0.72"))
        min_off = float(os.getenv("PYANNOTE_MIN_DURATION_OFF", "0.5"))
        try:
            params = pipeline.parameters(instantiated=True)
            params["clustering"]["threshold"] = clustering_thr
            params["segmentation"]["min_duration_off"] = min_off
            pipeline.instantiate(params)
            logger.info(
                "Pyannote tuned: clustering.threshold=%.3f, segmentation.min_duration_off=%.3f",
                clustering_thr, min_off,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Pyannote tuning skipped: %s", exc)
        _diarizer = pipeline
        logger.info("Loaded %s as %s", loaded_model, _diarizer_backend)
        return _diarizer
    except Exception as exc:  # noqa: BLE001
        _diarizer_load_error = f"{type(exc).__name__}: {exc}"
        logger.warning("Pyannote load failed: %s", _diarizer_load_error)
        return None


def _read_audio(buf: bytes | str) -> tuple[np.ndarray, int]:
    """Read audio from path or bytes -> mono float32 at TARGET_SR."""
    if isinstance(buf, str):
        if not os.path.exists(buf):
            raise HTTPException(status_code=400, detail=f"audio_path not found: {buf}")
        wav, sr = sf.read(buf, dtype="float32", always_2d=False)
    else:
        wav, sr = sf.read(io.BytesIO(buf), dtype="float32", always_2d=False)
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    if sr != TARGET_SR:
        wav_t = torch.from_numpy(wav).unsqueeze(0)
        wav_t = torchaudio.functional.resample(wav_t, sr, TARGET_SR)
        wav = wav_t.squeeze(0).numpy()
    duration = len(wav) / TARGET_SR
    if duration < MIN_SECONDS:
        raise HTTPException(status_code=400, detail=f"audio too short ({duration:.2f}s < {MIN_SECONDS}s)")
    if duration > MAX_SECONDS:
        raise HTTPException(status_code=400, detail=f"audio too long ({duration:.1f}s > {MAX_SECONDS}s)")
    return wav, TARGET_SR


def _embed(wav: np.ndarray) -> list[float]:
    embedder = _load_embedder()
    with torch.no_grad():
        signal = torch.from_numpy(wav).unsqueeze(0)
        emb = embedder({"waveform": signal, "sample_rate": TARGET_SR})
    emb = np.asarray(emb, dtype="float32").flatten()
    norm = np.linalg.norm(emb)
    if norm > 0:
        emb = emb / norm
    return emb.tolist()


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ---------- request / response models ----------


class HealthResponse(BaseModel):
    status: str
    device: str
    cuda_available: bool
    gpu_name: Optional[str] = None
    embedder_loaded: bool
    diarizer_available: bool
    diarizer_error: Optional[str] = None
    embedding_dim: int = 256
    embedder_device: str = EMBEDDER_DEVICE
    diarizer_device: str = DIARIZER_DEVICE


class EmbedResponse(BaseModel):
    embedding: list[float]
    embedding_dim: int
    duration_seconds: float
    model: str = EMBEDDING_MODEL


class DiarizeSegment(BaseModel):
    start: float
    end: float
    speaker: str  # e.g. "SPEAKER_00"
    embedding: Optional[list[float]] = None  # wespeaker embedding for the segment


class DiarizeResponse(BaseModel):
    segments: list[DiarizeSegment]
    num_speakers: int
    backend: str  # "pyannote-community-1", "pyannote-3.1", or "ecapa-cluster-fallback"
    duration_seconds: float


class IdentifyCandidate(BaseModel):
    speaker_id: int
    embedding: list[float]


class IdentifyRequest(BaseModel):
    embedding: list[float]
    candidates: list[IdentifyCandidate] = Field(default_factory=list)
    threshold: float = 0.55  # cosine similarity above this counts as a match


class IdentifyMatch(BaseModel):
    speaker_id: int
    similarity: float
    matched: bool


class IdentifyResponse(BaseModel):
    matches: list[IdentifyMatch]
    best_match: Optional[IdentifyMatch] = None


# ---------- routes ----------


@app.get("/health", response_model=HealthResponse)
def health():
    gpu_name = None
    if torch.cuda.is_available():
        try:
            gpu_name = torch.cuda.get_device_name(0)
        except Exception:  # noqa: BLE001
            gpu_name = None
    diarizer_available = HF_TOKEN is not None
    return HealthResponse(
        status="ok",
        device=DEVICE,
        cuda_available=torch.cuda.is_available(),
        gpu_name=gpu_name,
        embedder_loaded=_embedder is not None,
        diarizer_available=diarizer_available,
        diarizer_error=_diarizer_load_error,
        embedding_dim=256,
        embedder_device=EMBEDDER_DEVICE,
        diarizer_device=DIARIZER_DEVICE,
    )


@app.post("/embed", response_model=EmbedResponse)
async def embed(
    audio: Optional[UploadFile] = File(default=None),
    audio_path: Optional[str] = Form(default=None),
):
    """Embed a single short clip (typically 3-30s) into a 256-d wespeaker vector."""
    if audio is None and not audio_path:
        raise HTTPException(status_code=400, detail="provide audio (multipart) or audio_path (form field)")
    started = time.time()
    raw: bytes | str
    if audio_path:
        raw = audio_path
    else:
        raw = await audio.read()
    wav, _ = _read_audio(raw)
    async with _gpu_slot():
        emb = await asyncio.to_thread(_embed, wav)
    logger.info("/embed dim=%d took=%.2fs", len(emb), time.time() - started)
    return EmbedResponse(
        embedding=emb,
        embedding_dim=len(emb),
        duration_seconds=len(wav) / TARGET_SR,
    )


def _run_diarization_on_wav(
    wav: np.ndarray,
    *,
    return_embeddings: bool = True,
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
) -> DiarizeResponse:
    """Core diarization path shared by /diarize and /healthz/synthetic.

    Takes an already-loaded mono float32 waveform at TARGET_SR (use
    _read_audio() to get it). Returns the same DiarizeResponse shape /diarize
    returns. Falls back to single-speaker when pyannote can't be loaded so
    the caller always gets a usable response.
    """
    duration = len(wav) / TARGET_SR
    pipeline = _load_diarizer()
    if pipeline is None:
        # Graceful degradation — one segment, one wespeaker embedding for the
        # whole clip. Caller can still cluster across many segments using
        # /identify if they want.
        emb = _embed(wav) if return_embeddings else None
        return DiarizeResponse(
            segments=[DiarizeSegment(start=0.0, end=duration, speaker="SPEAKER_00", embedding=emb)],
            num_speakers=1,
            backend="ecapa-cluster-fallback",
            duration_seconds=duration,
        )

    # Pyannote path — pyannote pipelines expect a file path or a torchaudio
    # waveform tuple.
    waveform = torch.from_numpy(wav).unsqueeze(0)  # (1, T)

    diarize_kwargs: dict[str, int] = {}
    if num_speakers is not None:
        diarize_kwargs["num_speakers"] = int(num_speakers)
    else:
        if min_speakers is not None:
            diarize_kwargs["min_speakers"] = int(min_speakers)
        if max_speakers is not None:
            diarize_kwargs["max_speakers"] = int(max_speakers)
    logger.info(
        "diarize duration=%.1fs hints=%s threshold=env-default",
        duration,
        diarize_kwargs or "auto",
    )
    # Task #79 v2: wrap the top-level pipeline call in torch.inference_mode().
    # Pyannote uses inference_mode() internally at the Inference.infer() layer
    # but the top-level Pipeline.__call__ in 3.1 does not — this is
    # belt-and-suspenders against any autograd state pyannote may build during
    # diarization (clustering, segmentation post-processing). inference_mode()
    # is strictly more memory-efficient than no_grad() since it also disables
    # view tracking.
    with torch.inference_mode():
        result = pipeline(
            {"waveform": waveform, "sample_rate": TARGET_SR},
            **diarize_kwargs,
        )
    if hasattr(result, "exclusive_speaker_diarization"):
        diarization = result.exclusive_speaker_diarization
    elif hasattr(result, "speaker_diarization"):
        diarization = result.speaker_diarization
    else:
        diarization = result

    segments: list[DiarizeSegment] = []
    speakers_seen: set[str] = set()

    def add_segment(turn, speaker: str) -> None:
        speakers_seen.add(speaker)
        seg_emb = None
        if return_embeddings:
            start_idx = max(0, int(turn.start * TARGET_SR))
            end_idx = min(len(wav), int(turn.end * TARGET_SR))
            if end_idx - start_idx >= int(MIN_SECONDS * TARGET_SR):
                try:
                    seg_emb = _embed(wav[start_idx:end_idx])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("segment embed failed: %s", exc)
        segments.append(DiarizeSegment(
            start=float(turn.start),
            end=float(turn.end),
            speaker=speaker,
            embedding=seg_emb,
        ))

    try:
        for turn, speaker in diarization:
            add_segment(turn, speaker)
    except (TypeError, ValueError):
        segments = []
        speakers_seen = set()
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            add_segment(turn, speaker)

    response = DiarizeResponse(
        segments=segments,
        num_speakers=len(speakers_seen),
        backend=_diarizer_backend or "pyannote-3.1",
        duration_seconds=duration,
    )

    # Task #79 v2: periodic empty_cache() to release the caching allocator's
    # held memory back to the device. Pyannote 3.1 + autograd state legitimately
    # grows the allocator's reserved pool across calls; explicitly clearing
    # after every Nth request keeps the reserved pool from monotonically
    # growing in a long-running service. Set SPEAKER_EMPTY_CACHE_EVERY=0 to
    # disable, or raise N if throughput is impacted >10%.
    global _diarize_request_count
    _diarize_request_count += 1
    if (
        EMPTY_CACHE_EVERY > 0
        and torch.cuda.is_available()
        and _diarize_request_count % EMPTY_CACHE_EVERY == 0
    ):
        torch.cuda.empty_cache()

    return response


def _l2norm(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _run_chunked_diarization(
    wav: np.ndarray,
    *,
    return_embeddings: bool = True,
    num_speakers: Optional[int] = None,   # ignored in chunked mode (see below)
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
) -> DiarizeResponse:
    """Diarize a long clip by windowing + cross-window speaker stitch.

    Splits ``wav`` into CHUNK_WINDOW_SECONDS windows, runs the normal single-pass
    diarizer on each window (bounded peak GPU memory + time per window), then
    unifies each window's local SPEAKER_xx labels into one global set by matching
    pooled wespeaker centroids (cosine >= CHUNK_STITCH_THRESHOLD). Same person
    across windows -> one global label. Same DiarizeResponse shape as single-pass,
    timestamps offset to absolute position. Speaker-count hints are intentionally
    ignored — the global count emerges from the stitch. Embeddings are computed
    internally for stitching regardless of return_embeddings; they're only dropped
    from the OUTPUT when the caller asked not to receive them.
    """
    total = len(wav)
    win = max(1, int(CHUNK_WINDOW_SECONDS * TARGET_SR))
    min_samples = int(MIN_SECONDS * TARGET_SR)
    n_windows = max(1, (total + win - 1) // win)
    globals_: list[dict] = []  # [{label, centroid: np.ndarray|None, count: int}]
    out: list[DiarizeSegment] = []
    logger.info(
        "chunked diarize: %.1fs -> %d window(s) of %.0fs (stitch>=%.2f)",
        total / TARGET_SR, n_windows, CHUNK_WINDOW_SECONDS, CHUNK_STITCH_THRESHOLD,
    )
    for wi in range(n_windows):
        s = wi * win
        e = min(total, s + win)
        if e - s < min_samples:
            continue  # skip a tiny trailing sliver
        offset = s / TARGET_SR
        sub = _run_diarization_on_wav(wav[s:e], return_embeddings=True)
        # Pool one centroid per LOCAL speaker label in this window.
        local_embs: dict[str, list[np.ndarray]] = {}
        for seg in sub.segments:
            if seg.embedding:
                local_embs.setdefault(seg.speaker, []).append(
                    np.asarray(seg.embedding, dtype="float32")
                )
        local_to_global: dict[str, str] = {}
        for lbl, embs in local_embs.items():
            cen = _l2norm(np.mean(np.stack(embs), axis=0))
            best, best_sim = None, -1.0
            for g in globals_:
                if g["centroid"] is None:
                    continue
                sim = _cos(cen, g["centroid"])
                if sim > best_sim:
                    best_sim, best = sim, g
            logger.info(
                "stitch win=%d local=%s best_sim=%.3f thr=%.2f -> %s",
                wi, lbl, best_sim, CHUNK_STITCH_THRESHOLD,
                "merge" if (best is not None and best_sim >= CHUNK_STITCH_THRESHOLD) else "NEW",
            )
            if best is not None and best_sim >= CHUNK_STITCH_THRESHOLD:
                local_to_global[lbl] = best["label"]
                k = best["count"]
                best["centroid"] = _l2norm(best["centroid"] * k + cen)
                best["count"] = k + 1
            else:
                new_label = "SPEAKER_%02d" % len(globals_)
                globals_.append({"label": new_label, "centroid": cen, "count": 1})
                local_to_global[lbl] = new_label
        # Emit this window's segments with global labels + absolute timestamps.
        for seg in sub.segments:
            g_label = local_to_global.get(seg.speaker)
            if g_label is None:
                # local speaker had no usable embedding -> give it its own label
                g_label = "SPEAKER_%02d" % len(globals_)
                globals_.append({"label": g_label, "centroid": None, "count": 0})
                local_to_global[seg.speaker] = g_label
            out.append(DiarizeSegment(
                start=seg.start + offset,
                end=seg.end + offset,
                speaker=g_label,
                embedding=seg.embedding if return_embeddings else None,
            ))
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    out.sort(key=lambda x: x.start)
    logger.info("chunked diarize done: %d segments, %d global speakers", len(out), len(globals_))
    return DiarizeResponse(
        segments=out,
        num_speakers=len(globals_),
        backend=(_diarizer_backend or "pyannote-3.1") + "-chunked",
        duration_seconds=total / TARGET_SR,
    )


@app.post("/diarize", response_model=DiarizeResponse)
async def diarize(
    audio: Optional[UploadFile] = File(default=None),
    audio_path: Optional[str] = Form(default=None),
    return_embeddings: bool = Form(default=True),
    num_speakers: Optional[int] = Form(default=None),
    min_speakers: Optional[int] = Form(default=None),
    max_speakers: Optional[int] = Form(default=None),
    clustering_threshold: Optional[float] = Form(default=None),
):
    """Diarize. Uses Pyannote community-1 if HUGGINGFACE_TOKEN is set; otherwise
    falls back to single-speaker pseudo-diarization (one segment for
    the whole clip) so callers always get a usable response shape."""
    if audio is None and not audio_path:
        raise HTTPException(status_code=400, detail="provide audio or audio_path")

    # Task #79 v2: per-request clustering threshold overrides are no longer
    # supported. Calling pipeline.instantiate() per request appears to retain
    # the previous parameter graph in pyannote 3.1/4.x and contributes to the
    # VRAM accumulation that necessitates the 8h restart workaround. The
    # threshold is now fixed at startup via PYANNOTE_CLUSTERING_THRESHOLD env
    # var (default 0.72; midboy2 deploy sets 0.78). Callers who need a
    # different value must redeploy the service with the new env value.
    if clustering_threshold is not None:
        raise HTTPException(
            status_code=400,
            detail=(
                "per-request clustering_threshold override is no longer supported "
                "(task #79 v2: prevents pyannote VRAM accumulation). "
                "Set PYANNOTE_CLUSTERING_THRESHOLD env on the speaker-svc deploy."
            ),
        )

    raw: bytes | str
    if audio_path:
        raw = audio_path
    else:
        raw = await audio.read()
    wav, _ = _read_audio(raw)
    # Offload the blocking pyannote pass off the event loop so the single
    # uvicorn worker can still service /health (and queue a second request)
    # while a diarization runs — otherwise a long meeting stalls the loop and
    # can trip a false-unhealthy healthcheck. (v3.29.0)
    duration = len(wav) / TARGET_SR
    async with _gpu_slot():
        if duration > CHUNK_THRESHOLD_SECONDS:
            return await asyncio.to_thread(
                _run_chunked_diarization,
                wav,
                return_embeddings=return_embeddings,
                num_speakers=num_speakers,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )
        return await asyncio.to_thread(
            _run_diarization_on_wav,
            wav,
            return_embeddings=return_embeddings,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )


@app.post("/identify", response_model=IdentifyResponse)
def identify(req: IdentifyRequest):
    """Score a query embedding against candidate enrolled-speaker embeddings.

    Pure cosine similarity. Caller is responsible for picking the right
    threshold for their environment (we default to 0.55 which is a reasonable
    starting point for conference-room mics)."""
    if not req.embedding:
        raise HTTPException(status_code=400, detail="empty embedding")
    if not req.candidates:
        return IdentifyResponse(matches=[])

    q = np.asarray(req.embedding, dtype="float32")
    matches: list[IdentifyMatch] = []
    for cand in req.candidates:
        c = np.asarray(cand.embedding, dtype="float32")
        if c.shape != q.shape:
            # Skip mismatched-dim embeddings (e.g. legacy ECAPA enrollments)
            matches.append(IdentifyMatch(speaker_id=cand.speaker_id, similarity=0.0, matched=False))
            continue
        sim = _cos(q, c)
        matches.append(IdentifyMatch(
            speaker_id=cand.speaker_id,
            similarity=sim,
            matched=sim >= req.threshold,
        ))

    matches.sort(key=lambda m: m.similarity, reverse=True)
    best = matches[0] if matches else None
    return IdentifyResponse(matches=matches, best_match=best)


# ---------- synthetic probe (task #105) ----------
#
# The standard /health endpoint only confirms the HTTP server is up and reports
# whether models *can* be loaded. It does not exercise the full diarization
# pipeline. A few days ago we hit a class of failure where pyannote returned
# collapsed clusters (everyone-as-one-speaker) and the only fix was a container
# restart at the SAME clustering threshold — i.e. pipeline-state degradation.
# That class of failure is invisible to /health.
#
# /healthz/synthetic runs a bundled known-good 2-speaker WAV through the exact
# same code path normal /diarize requests hit (_run_diarization_on_wav) and
# asserts the result matches the ground truth in the sibling .meta.json. If
# the pipeline degrades into a 1-speaker (or >2-speaker) result, the probe
# returns 503 and Docker's healthcheck flags the container as unhealthy.

FIXTURE_DIR = Path(__file__).parent / "test_fixtures"
SYNTHETIC_FIXTURE_PATH = FIXTURE_DIR / "synthetic_2speaker.wav"
SYNTHETIC_META_PATH = FIXTURE_DIR / "synthetic_2speaker.meta.json"


def _load_synthetic_meta() -> dict:
    """Read the fixture's ground-truth manifest. Cached per-process via lru-style
    via module global to avoid disk reads on every probe."""
    global _synthetic_meta_cache
    try:
        return _synthetic_meta_cache  # type: ignore[name-defined]
    except NameError:
        pass
    if not SYNTHETIC_META_PATH.exists():
        raise FileNotFoundError(f"synthetic meta missing at {SYNTHETIC_META_PATH}")
    _synthetic_meta_cache = json.loads(SYNTHETIC_META_PATH.read_text())  # type: ignore[name-defined]
    return _synthetic_meta_cache  # type: ignore[name-defined]


def _centroid_cosine_distance(segments: list[DiarizeSegment]) -> Optional[float]:
    """Compute cosine distance between the two cluster centroids, given a
    diarization result with embeddings. Returns None when there are not
    exactly 2 clusters or any embeddings are missing."""
    by_speaker: dict[str, list[np.ndarray]] = {}
    for seg in segments:
        if seg.embedding is None:
            continue
        by_speaker.setdefault(seg.speaker, []).append(
            np.asarray(seg.embedding, dtype="float32")
        )
    if len(by_speaker) != 2:
        return None
    centroids = [np.mean(np.stack(v), axis=0) for v in by_speaker.values()]
    if any(v.size == 0 for v in centroids):
        return None
    sim = _cos(centroids[0], centroids[1])
    return 1.0 - sim


@app.get("/healthz/synthetic")
async def healthz_synthetic():
    """Full-pipeline probe.

    Runs the bundled 2-speaker WAV (test_fixtures/synthetic_2speaker.wav)
    through the actual diarization pipeline _run_diarization_on_wav() and
    asserts the result matches the manifest in synthetic_2speaker.meta.json:

      * num_speakers == expected_speaker_count (default 2)
      * segments > 1 (single-cluster degeneracy guard)
      * cosine distance between cluster centroids >= expected_min_embedding_distance

    Returns 200 OK with { status: ok, ... } on pass.
    Returns 503 Service Unavailable with { status: degraded, reason: "...", ... }
    on any failure. Reason values: fixture_missing, fixture_unreadable,
    diarization_threw, speaker_count_mismatch, segments_too_few,
    embedding_distance_too_low.
    """
    start = time.monotonic()
    checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Step 1: load the fixture + manifest.
    if not SYNTHETIC_FIXTURE_PATH.exists():
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": "fixture_missing",
                "fixture_path": str(SYNTHETIC_FIXTURE_PATH),
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "checked_at": checked_at,
            },
        )
    try:
        expected = _load_synthetic_meta()
        wav, _ = _read_audio(str(SYNTHETIC_FIXTURE_PATH))
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": "fixture_unreadable",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "checked_at": checked_at,
            },
        )

    expected_speaker_count = int(expected.get("expected_speaker_count", 2))
    min_emb_distance = float(expected.get("expected_min_embedding_distance", 0.4))
    fixture_name = expected.get("filename", SYNTHETIC_FIXTURE_PATH.name)

    # Step 2: run the actual pipeline. ANY exception => degraded.
    try:
        result = _run_diarization_on_wav(wav, return_embeddings=True)
    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error(
            "/healthz/synthetic diarization_threw type=%s err=%s elapsed_ms=%d",
            type(exc).__name__, exc, elapsed_ms,
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": "diarization_threw",
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": elapsed_ms,
                "fixture": fixture_name,
                "checked_at": checked_at,
            },
        )

    elapsed_ms = int((time.monotonic() - start) * 1000)
    speaker_count = result.num_speakers
    segment_count = len(result.segments)

    # Step 3: speaker count must match ground truth (catches collapsed-cluster
    # everyone-as-one-speaker degeneracy, and over-splitting too).
    if speaker_count != expected_speaker_count:
        logger.error(
            "/healthz/synthetic speaker_count_mismatch expected=%d actual=%d segments=%d elapsed_ms=%d",
            expected_speaker_count, speaker_count, segment_count, elapsed_ms,
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": "speaker_count_mismatch",
                "expected": expected_speaker_count,
                "speaker_count_actual": speaker_count,
                "segments": segment_count,
                "elapsed_ms": elapsed_ms,
                "fixture": fixture_name,
                "backend": result.backend,
                "checked_at": checked_at,
            },
        )

    # Step 4: segments must be > 1. A 2-speaker WAV producing a single segment
    # spanning the whole file would mean the segmenter degraded even if the
    # cluster count came out by coincidence.
    if segment_count < 2:
        logger.error(
            "/healthz/synthetic segments_too_few count=%d elapsed_ms=%d",
            segment_count, elapsed_ms,
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": "segments_too_few",
                "speaker_count_actual": speaker_count,
                "segments": segment_count,
                "elapsed_ms": elapsed_ms,
                "fixture": fixture_name,
                "backend": result.backend,
                "checked_at": checked_at,
            },
        )

    # Step 5: optional embedding-distance check. If embeddings are present,
    # the two cluster centroids should be far apart for our synthetic fixture
    # (male + female TTS voices land at ~0.97 cosine distance in practice).
    # We require >= expected_min_embedding_distance (default 0.4) which is
    # conservative — anything tighter would catch wespeaker degradation too.
    distance: Optional[float] = _centroid_cosine_distance(result.segments)
    if distance is not None and distance < min_emb_distance:
        logger.error(
            "/healthz/synthetic embedding_distance_too_low distance=%.4f min=%.4f elapsed_ms=%d",
            distance, min_emb_distance, elapsed_ms,
        )
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "reason": "embedding_distance_too_low",
                "embedding_distance": round(distance, 4),
                "embedding_distance_required": min_emb_distance,
                "speaker_count_actual": speaker_count,
                "segments": segment_count,
                "elapsed_ms": elapsed_ms,
                "fixture": fixture_name,
                "backend": result.backend,
                "checked_at": checked_at,
            },
        )

    # All checks passed.
    logger.info(
        "/healthz/synthetic ok speaker_count=%d segments=%d distance=%s elapsed_ms=%d backend=%s",
        speaker_count,
        segment_count,
        f"{distance:.4f}" if distance is not None else "n/a",
        elapsed_ms,
        result.backend,
    )
    payload: dict[str, object] = {
        "status": "ok",
        "speaker_count": speaker_count,
        "segments": segment_count,
        "elapsed_ms": elapsed_ms,
        "fixture": fixture_name,
        "backend": result.backend,
        "checked_at": checked_at,
    }
    if distance is not None:
        payload["embedding_distance"] = round(distance, 4)
        payload["embedding_distance_required"] = min_emb_distance
    return payload


@app.on_event("startup")
def _warm():
    if EAGER_LOAD_EMBEDDER:
        try:
            _load_embedder()
            logger.info("wespeaker warmed on %s", EMBEDDER_DEVICE)
        except Exception as exc:  # noqa: BLE001
            logger.error("wespeaker warm failed: %s", exc)
    else:
        logger.info("wespeaker warm skipped; lazy load enabled")
    if HF_TOKEN and EAGER_LOAD_DIARIZER:
        _load_diarizer()  # warm pyannote if explicitly requested
    elif HF_TOKEN:
        logger.info("pyannote warm skipped; lazy load enabled")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8889, reload=False)
