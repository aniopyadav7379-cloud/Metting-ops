"""vibevoice-svc — FastAPI wrapper around Microsoft VibeVoice for long-form, multi-speaker TTS.

Endpoints
---------
GET  /health   — liveness, model status, GPU info
GET  /voices   — list available voice presets (built-in)
POST /tts      — synthesize a single-speaker clip from {text, voice_id, format}
POST /podcast  — synthesize a multi-speaker conversation from {script, voices, format}

Runtime
-------
- Lives on midboy1 P40 #1 (Tesla P40, 24 GB).
- VibeVoice-1.5B uses ~6-8 GB VRAM; plenty of headroom.
- Returns audio bytes inline; no remote storage involved here.
- Caller (UC Meeting-Ops backend) is responsible for persisting outputs to disk.

Why this layout
---------------
The Microsoft VibeVoice repo ships a `voices/` directory with reference WAVs that
anchor each preset. The pipeline is:
    text  ->  tokenize  ->  acoustic + semantic tokenizers  ->  diffusion head  ->  24 kHz wav
For multi-speaker mode the model itself handles turn-taking when the script is
formatted as `Speaker 1: hello\nSpeaker 2: hi`. We expose that natively via /podcast.
"""
from __future__ import annotations

import io
import logging
import os
import time
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

logger = logging.getLogger("vibevoice-svc")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

# -------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.bfloat16 if DEVICE == "cuda" else torch.float32

MODEL_NAME = os.getenv("VIBEVOICE_MODEL", "vibevoice/VibeVoice-1.5B")
MODEL_CACHE_DIR = os.getenv("VIBEVOICE_CACHE", "/models/vibevoice")
VOICES_DIR = os.getenv("VIBEVOICE_VOICES_DIR", "/app/voices")
SAMPLE_RATE = 24000
DEFAULT_CFG_SCALE = float(os.getenv("VIBEVOICE_CFG_SCALE", "1.3"))
DEFAULT_DDPM_STEPS = int(os.getenv("VIBEVOICE_DDPM_STEPS", "10"))
LOAD_AT_STARTUP = os.getenv("VIBEVOICE_EAGER_LOAD", "true").lower() == "true"

# -------------------------------------------------------------------------
# Lazy-loaded model state
# -------------------------------------------------------------------------
_model = None
_processor = None
_load_error: Optional[str] = None
_load_lock = threading.Lock()


def _resolve_voice_paths() -> dict[str, str]:
    """Map of voice_id -> absolute WAV path.

    The VibeVoice repo ships a `voices/` directory with sample WAVs named like
    `en-Alice_woman.wav`. We expose those as voice IDs (e.g. `alice`, `frank`).
    Falls back to whatever is on disk so an operator can drop in extra voices.
    """
    base = Path(VOICES_DIR)
    if not base.exists():
        return {}
    voices = {}
    for wav in sorted(base.glob("*.wav")):
        # `en-Alice_woman.wav` -> `alice`
        # `en-Frank_man.wav`   -> `frank`
        stem = wav.stem
        # tolerate `en-Alice_woman` and `Alice` etc.
        token = stem.split("-", 1)[1] if "-" in stem else stem
        token = token.split("_", 1)[0].lower()
        voices.setdefault(token, str(wav.resolve()))
        # also accept the raw stem so callers can be explicit
        voices.setdefault(stem.lower(), str(wav.resolve()))
    return voices


def _builtin_voice_ids() -> list[str]:
    """Stable, deduped list of voice IDs for /voices."""
    voices = _resolve_voice_paths()
    seen, out = set(), []
    for k in voices:
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _load_model():
    """Load VibeVoice once and cache. Safe to call concurrently."""
    global _model, _processor, _load_error
    if _model is not None and _processor is not None:
        return _model, _processor

    with _load_lock:
        if _model is not None and _processor is not None:
            return _model, _processor
        try:
            # Imported lazily so `--help` etc. doesn't trigger a CUDA init.
            from vibevoice.modular.modeling_vibevoice_inference import (
                VibeVoiceForConditionalGenerationInference,
            )
            from vibevoice.processor.vibevoice_processor import VibeVoiceProcessor

            logger.info("Loading VibeVoice model %s on %s (dtype=%s)", MODEL_NAME, DEVICE, DTYPE)
            t0 = time.time()
            _processor = VibeVoiceProcessor.from_pretrained(
                MODEL_NAME, cache_dir=MODEL_CACHE_DIR
            )
            model = VibeVoiceForConditionalGenerationInference.from_pretrained(
                MODEL_NAME,
                cache_dir=MODEL_CACHE_DIR,
                torch_dtype=DTYPE,
                device_map="cuda" if DEVICE == "cuda" else None,
            )
            model.eval()
            try:
                model.set_ddpm_inference_steps(num_steps=DEFAULT_DDPM_STEPS)
            except Exception:  # noqa: BLE001
                # Older VibeVoice builds may not expose this — silently skip.
                pass
            _model = model
            logger.info("VibeVoice loaded in %.1fs", time.time() - t0)
        except Exception as exc:  # noqa: BLE001
            _load_error = f"{type(exc).__name__}: {exc}"
            logger.exception("VibeVoice load failed: %s", _load_error)
            raise
    return _model, _processor


def _voice_path_for(voice_id: Optional[str]) -> str:
    """Resolve a voice_id to an on-disk WAV. Falls back to the first voice."""
    voices = _resolve_voice_paths()
    if not voices:
        raise HTTPException(status_code=503, detail=f"No voices available in {VOICES_DIR}")
    if not voice_id:
        return next(iter(voices.values()))
    norm = voice_id.lower()
    if norm in voices:
        return voices[norm]
    # tolerate `en-Alice_woman` style passed verbatim
    if voice_id in voices:
        return voices[voice_id]
    raise HTTPException(status_code=400, detail=f"Unknown voice_id '{voice_id}'. Available: {sorted(voices.keys())}")


def _format_script(turns: list[dict]) -> tuple[str, list[str]]:
    """Convert [{speaker_id, text}, ...] into VibeVoice's expected script format.

    VibeVoice expects the prompt as:
        Speaker 1: line one
        Speaker 2: line two
        Speaker 1: line three
    where speakers are referenced by ordinal index, not by name. We also need a
    parallel list of voice_paths in the same speaker order.

    Returns (formatted_text, [voice_path_for_speaker_1, voice_path_for_speaker_2, ...]).
    Caller is responsible for filling out voice_paths with the right preset WAVs.
    """
    if not turns:
        raise HTTPException(status_code=400, detail="empty script")
    # Stable speaker order keyed by first appearance.
    order: list[str] = []
    for turn in turns:
        sid = str(turn.get("speaker_id") or "").strip()
        if not sid:
            raise HTTPException(status_code=400, detail="every turn needs a speaker_id")
        if sid not in order:
            order.append(sid)
    # Render the script using `Speaker N: ...` lines so VibeVoice's tokenizer
    # picks up the multi-speaker pattern natively.
    lines = []
    for turn in turns:
        sid = str(turn.get("speaker_id") or "").strip()
        text = (turn.get("text") or "").strip()
        if not text:
            continue
        idx = order.index(sid) + 1  # 1-based for the model
        lines.append(f"Speaker {idx}: {text}")
    if not lines:
        raise HTTPException(status_code=400, detail="script has no usable text")
    return "\n".join(lines), order


def _generate(
    *,
    text: str,
    voice_paths: list[str],
    cfg_scale: float = DEFAULT_CFG_SCALE,
    max_new_tokens: Optional[int] = None,
) -> np.ndarray:
    """Run the model and return a mono float32 24 kHz waveform."""
    model, processor = _load_model()

    inputs = processor(
        text=[text],
        voice_samples=[voice_paths],
        padding=True,
        return_tensors="pt",
        return_attention_mask=True,
    )

    if DEVICE == "cuda":
        for k, v in inputs.items():
            if isinstance(v, torch.Tensor):
                inputs[k] = v.to("cuda")

    gen_kwargs = dict(
        cfg_scale=cfg_scale,
        tokenizer=processor.tokenizer,
        generation_config={"do_sample": False},
        verbose=False,
    )
    if max_new_tokens:
        gen_kwargs["max_new_tokens"] = max_new_tokens

    with torch.inference_mode():
        outputs = model.generate(**inputs, **gen_kwargs)

    # `outputs.speech_outputs` is a list of tensors (one per item in the batch).
    speech = None
    if hasattr(outputs, "speech_outputs") and outputs.speech_outputs:
        speech = outputs.speech_outputs[0]
    elif hasattr(outputs, "audio") and outputs.audio is not None:
        speech = outputs.audio
    if speech is None:
        raise HTTPException(status_code=500, detail="Model returned no audio")

    if isinstance(speech, torch.Tensor):
        wav = speech.detach().to("cpu").float().numpy()
    else:
        wav = np.asarray(speech, dtype="float32")
    if wav.ndim > 1:
        # If the pipeline returns shape (1, T) or (T, 1) — flatten to (T,)
        wav = np.squeeze(wav)
        if wav.ndim > 1:
            wav = wav.mean(axis=0)
    return wav.astype("float32")


def _encode_audio(wav: np.ndarray, fmt: str) -> bytes:
    """Encode a float32 24 kHz waveform to WAV or MP3 bytes."""
    fmt = (fmt or "wav").lower()
    if fmt not in ("wav", "mp3"):
        raise HTTPException(status_code=400, detail=f"Unsupported format '{fmt}'. Use 'wav' or 'mp3'.")
    buf = io.BytesIO()
    if fmt == "wav":
        sf.write(buf, wav, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    # MP3 — soundfile may not have libmp3lame, fall back to pydub via lame.
    try:
        sf.write(buf, wav, SAMPLE_RATE, format="MP3")
        return buf.getvalue()
    except Exception:
        pass
    try:
        from pydub import AudioSegment

        wav_buf = io.BytesIO()
        sf.write(wav_buf, wav, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        wav_buf.seek(0)
        seg = AudioSegment.from_wav(wav_buf)
        mp3_buf = io.BytesIO()
        seg.export(mp3_buf, format="mp3", bitrate="128k")
        return mp3_buf.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.warning("MP3 encoding fell through to WAV: %s", exc)
        # Last resort: return WAV bytes; client can transcode.
        wav_buf = io.BytesIO()
        sf.write(wav_buf, wav, SAMPLE_RATE, format="WAV", subtype="PCM_16")
        return wav_buf.getvalue()


# -------------------------------------------------------------------------
# Request / response models
# -------------------------------------------------------------------------
class HealthResponse(BaseModel):
    status: str
    device: str
    cuda_available: bool
    gpu_name: Optional[str] = None
    model_name: str
    model_loaded: bool
    load_error: Optional[str] = None
    voices_available: int
    sample_rate: int = SAMPLE_RATE
    version: str = "0.1.0"


class VoiceListItem(BaseModel):
    voice_id: str
    path: str


class VoicesResponse(BaseModel):
    voices: list[VoiceListItem]
    sample_rate: int = SAMPLE_RATE


class TTSRequest(BaseModel):
    text: str
    voice_id: Optional[str] = None
    voices: Optional[list[str]] = None  # alias for voice_id (list of one)
    format: str = "mp3"
    cfg_scale: float = DEFAULT_CFG_SCALE


class PodcastTurn(BaseModel):
    speaker_id: str = Field(..., description="Stable speaker identifier referenced in `voices`")
    text: str


class PodcastRequest(BaseModel):
    script: list[PodcastTurn]
    voices: dict[str, str] = Field(
        default_factory=dict,
        description="Map of speaker_id -> voice preset id (e.g. {'host':'alice','guest':'frank'}).",
    )
    format: str = "mp3"
    cfg_scale: float = DEFAULT_CFG_SCALE


# -------------------------------------------------------------------------
# FastAPI app
# -------------------------------------------------------------------------
app = FastAPI(title="vibevoice-svc", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health():
    gpu_name = None
    if torch.cuda.is_available():
        try:
            gpu_name = torch.cuda.get_device_name(0)
        except Exception:  # noqa: BLE001
            gpu_name = None
    return HealthResponse(
        status="ok" if (_load_error is None) else "degraded",
        device=DEVICE,
        cuda_available=torch.cuda.is_available(),
        gpu_name=gpu_name,
        model_name=MODEL_NAME,
        model_loaded=_model is not None and _processor is not None,
        load_error=_load_error,
        voices_available=len(_builtin_voice_ids()),
    )


@app.get("/voices", response_model=VoicesResponse)
def voices():
    paths = _resolve_voice_paths()
    return VoicesResponse(
        voices=[VoiceListItem(voice_id=k, path=v) for k, v in paths.items()],
    )


@app.post("/tts")
def tts(req: TTSRequest):
    voice = req.voice_id or (req.voices[0] if req.voices else None)
    voice_path = _voice_path_for(voice)
    formatted = f"Speaker 1: {req.text.strip()}"
    started = time.time()
    wav = _generate(text=formatted, voice_paths=[voice_path], cfg_scale=req.cfg_scale)
    encoded = _encode_audio(wav, req.format)
    logger.info(
        "/tts voice=%s format=%s len=%.2fs took=%.1fs",
        voice or "default",
        req.format,
        len(wav) / SAMPLE_RATE,
        time.time() - started,
    )
    media = "audio/mpeg" if req.format.lower() == "mp3" else "audio/wav"
    return Response(content=encoded, media_type=media)


@app.post("/podcast")
def podcast(req: PodcastRequest):
    if not req.script:
        raise HTTPException(status_code=400, detail="empty script")
    turns = [t.model_dump() for t in req.script]
    formatted, speaker_order = _format_script(turns)

    # Build voice path list in the same order the speakers appear.
    voice_paths = []
    for sid in speaker_order:
        voice_id = req.voices.get(sid)
        voice_paths.append(_voice_path_for(voice_id))

    started = time.time()
    wav = _generate(text=formatted, voice_paths=voice_paths, cfg_scale=req.cfg_scale)
    encoded = _encode_audio(wav, req.format)
    logger.info(
        "/podcast speakers=%d turns=%d format=%s len=%.2fs took=%.1fs",
        len(speaker_order),
        len(req.script),
        req.format,
        len(wav) / SAMPLE_RATE,
        time.time() - started,
    )
    media = "audio/mpeg" if req.format.lower() == "mp3" else "audio/wav"
    return Response(content=encoded, media_type=media)


@app.on_event("startup")
def _warm():
    if not LOAD_AT_STARTUP:
        logger.info("VIBEVOICE_EAGER_LOAD=false; model will load on first request.")
        return
    try:
        _load_model()
    except Exception as exc:  # noqa: BLE001
        # Don't fail startup — /health will report load_error and the next
        # request will surface the real exception.
        logger.error("Eager VibeVoice load failed: %s", exc)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8882, reload=False)
