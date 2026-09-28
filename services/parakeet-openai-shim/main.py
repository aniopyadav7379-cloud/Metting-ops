"""parakeet-openai-shim — OpenAI-compatible STT shim in front of parakeet-svc.

Why this exists
---------------
The UC public inference gateway (LiteLLM) proxies OpenAI *audio* routes — i.e.
`POST /v1/audio/transcriptions` shaped like the OpenAI Whisper API — so STT can
be offered "like an AI provider" alongside chat/embeddings. Parakeet's native
service (`parakeet-svc`) speaks a different, UC-internal contract
(`POST /transcribe`, multipart field `audio`, query params, a richer JSON body).

This shim is a thin, stateless translator:

    OpenAI client / gateway
        --> POST /v1/audio/transcriptions   (this shim)
            --> POST {PARAKEET_URL}/transcribe   (parakeet-svc)
        <-- OpenAI-shaped response (json / verbose_json / text)
    (+ one best-effort usage-metering event per success)

It mirrors the request shape used by the backend's Parakeet provider
(`backend/services/providers/impl_stt.py` :: LocalParakeetProvider) so the
on-the-wire contract to parakeet-svc is identical, including the timeout
scaling (base 60s + 2s/MB).

Metering
--------
After a SUCCESSFUL transcription, the shim emits exactly ONE usage event to the
UC metering bridge (`{service_type:"stt", org_id, quantity:<audio_seconds>}`,
auth via `X-Metering-Key`). LiteLLM does NOT meter audio routes for us, so the
shim owns this. Metering is strictly best-effort and non-blocking: it is fired
*after* the response is computed, off the response critical path, and a
metering failure (or ops-center being down) never fails or delays a
transcription. If the caller/gateway didn't inject the org header, metering is
simply skipped (logged) — STT still succeeds.

Everything is configured via env (see README.md) with sensible defaults.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

logger = logging.getLogger("parakeet-openai-shim")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

SERVICE_VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Config (all via env, with sensible defaults)
# ---------------------------------------------------------------------------

# parakeet-svc base URL. Default is the bigboy/midboy1 Tailscale address +
# parakeet-svc port (8881). In-cluster this is typically http://meet-parakeet-svc:8881.
PARAKEET_URL = os.getenv("PARAKEET_URL", "http://meet-parakeet-svc:8881").rstrip("/")

# Port this shim listens on.
SHIM_PORT = int(os.getenv("SHIM_PORT", "8011"))

# Model name we report back in OpenAI responses (cosmetic; parakeet-svc picks
# the real model). Also the name the gateway/litellm advertises.
STT_MODEL_NAME = os.getenv("STT_MODEL_NAME", "parakeet")

# --- Metering (LOCKED contract from the suite dev) ---
# Full URL of the UC metering bridge, e.g.
#   internal: http://ops-center:8084/api/v1/metered/report
#   public:   https://api.unicorncommander.ai/api/v1/metered/report
# If unset/empty, metering is disabled (the shim still transcribes).
METERED_REPORT_URL = os.getenv("METERED_REPORT_URL", "").strip()

# Federation key sent as the X-Metering-Key header to the metering bridge.
FEDERATION_KEY = os.getenv("FEDERATION_KEY", "").strip()

# Request header (injected by the gateway/caller) that carries the org id used
# for usage attribution. Absent -> metering is skipped, never an error.
ORG_HEADER = os.getenv("ORG_HEADER", "X-Org-Id")

# --- Timeout knobs ---
# Forward-to-Parakeet timeout scales with upload size (mirrors impl_stt.py:
# LocalParakeetProvider uses base=60.0, per_mb=2.0). Tunable via env.
PARAKEET_TIMEOUT_BASE = float(os.getenv("PARAKEET_TIMEOUT_BASE", "60.0"))
PARAKEET_TIMEOUT_PER_MB = float(os.getenv("PARAKEET_TIMEOUT_PER_MB", "2.0"))

# Metering call timeout — deliberately short; this must never delay STT.
METERING_TIMEOUT = float(os.getenv("METERING_TIMEOUT", "3.0"))


def _parakeet_timeout_for_bytes(size_bytes: int) -> float:
    """Scale the forward timeout with upload size: base + per_mb * MB.

    Mirrors `_http_timeout_for_path(base=60, per_mb=2)` in the backend's
    Parakeet provider so this shim and the in-process path agree on how long
    parakeet-svc is allowed to take for a given clip.
    """
    size_mb = max(0.0, size_bytes) / (1024 * 1024)
    return max(PARAKEET_TIMEOUT_BASE, PARAKEET_TIMEOUT_BASE + size_mb * PARAKEET_TIMEOUT_PER_MB)


# ---------------------------------------------------------------------------
# OpenAI-style error helper
# ---------------------------------------------------------------------------


def _openai_error(message: str, status_code: int, err_type: str = "api_error") -> JSONResponse:
    """Return an OpenAI-shaped error body: {"error": {"message", "type"}}."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "code": status_code}},
    )


# ---------------------------------------------------------------------------
# Response mapping: Parakeet JSON -> OpenAI Whisper-shaped responses
# ---------------------------------------------------------------------------


def _map_json(payload: dict[str, Any]) -> dict[str, Any]:
    """response_format=json -> {"text": "..."} (the minimal OpenAI shape)."""
    return {"text": (payload.get("text") or "").strip()}


def _map_verbose_json(payload: dict[str, Any], language_fallback: str) -> dict[str, Any]:
    """response_format=verbose_json -> OpenAI verbose transcription object.

    Maps Parakeet segments to OpenAI segment objects. OpenAI segments carry
    more fields (tokens/temperature/avg_logprob/...) than Parakeet provides;
    we populate the universally-present ones (id, seek, start, end, text) which
    is what virtually all OpenAI-SDK consumers read, and pass through word-level
    timestamps under `words` when Parakeet returned them.
    """
    segments_out: list[dict[str, Any]] = []
    for i, seg in enumerate(payload.get("segments") or []):
        segments_out.append(
            {
                "id": i,
                "seek": 0,
                "start": float(seg.get("start") or 0.0),
                "end": float(seg.get("end") or 0.0),
                "text": (seg.get("text") or "").strip(),
                # OpenAI parity fields — Parakeet doesn't supply these, so we
                # emit harmless defaults rather than omit (some strict clients
                # expect the keys to exist).
                "tokens": [],
                "temperature": 0.0,
                "avg_logprob": 0.0,
                "compression_ratio": 0.0,
                "no_speech_prob": 0.0,
            }
        )

    words_out: list[dict[str, Any]] = []
    for w in payload.get("words") or []:
        words_out.append(
            {
                "word": (w.get("word") or w.get("text") or "").strip(),
                "start": float(w.get("start") or 0.0),
                "end": float(w.get("end") or 0.0),
            }
        )

    out: dict[str, Any] = {
        "task": "transcribe",
        "language": payload.get("language") or language_fallback,
        "duration": float(payload.get("duration") or 0.0),
        "text": (payload.get("text") or "").strip(),
        "segments": segments_out,
    }
    if words_out:
        out["words"] = words_out
    return out


# ---------------------------------------------------------------------------
# Metering — strictly best-effort, fired after the response is computed
# ---------------------------------------------------------------------------


async def _emit_metering(org_id: Optional[str], audio_seconds: Optional[float]) -> None:
    """Emit exactly ONE usage event for a successful transcription.

    Contract (LOCKED):
        POST {METERED_REPORT_URL}
        headers: X-Metering-Key: {FEDERATION_KEY}
        body:    {"service_type": "stt", "org_id": <org>, "quantity": <seconds>}

    Rules:
      * Skipped (warning logged) if the org header was absent — never an error.
      * Skipped if METERED_REPORT_URL is not configured.
      * Wrapped in try/except with a short timeout; ANY failure is swallowed.
        A metering failure must never fail or delay the transcription.
    """
    if not METERED_REPORT_URL:
        logger.debug("metering disabled (METERED_REPORT_URL unset) — skipping")
        return
    if not org_id:
        logger.warning(
            "metering skipped: no org id (header %s absent on request)", ORG_HEADER
        )
        return

    body: dict[str, Any] = {"service_type": "stt", "org_id": org_id}
    # quantity = audio duration in seconds. Omit if Parakeet didn't report a
    # usable duration rather than send a bogus 0.
    if audio_seconds is not None and audio_seconds > 0:
        body["quantity"] = audio_seconds

    headers = {"X-Metering-Key": FEDERATION_KEY} if FEDERATION_KEY else {}

    try:
        async with httpx.AsyncClient(timeout=METERING_TIMEOUT) as client:
            resp = await client.post(METERED_REPORT_URL, json=body, headers=headers)
        if resp.status_code >= 400:
            logger.warning(
                "metering report returned %s: %s",
                resp.status_code,
                (resp.text or "")[:200],
            )
        else:
            logger.info(
                "metered stt: org=%s quantity=%s -> %s",
                org_id,
                body.get("quantity"),
                resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001 — metering must never raise upward
        logger.warning("metering report failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="parakeet-openai-shim", version=SERVICE_VERSION)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness + effective config (no upstream call — stays cheap)."""
    return {
        "ok": True,
        "status": "ok",
        "service": "parakeet-openai-shim",
        "version": SERVICE_VERSION,
        "parakeet_url": PARAKEET_URL,
        "model_name": STT_MODEL_NAME,
        "metering_enabled": bool(METERED_REPORT_URL),
        "org_header": ORG_HEADER,
    }


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: Optional[str] = Form(None),
    language: Optional[str] = Form(None),
    response_format: str = Form("json"),
    temperature: Optional[float] = Form(None),  # accepted + ignored (Parakeet has no sampling temp)
):
    """OpenAI Whisper-compatible transcription endpoint.

    Forwards the upload to parakeet-svc `/transcribe`, then shapes the result
    to the requested `response_format` (json | verbose_json | text). After a
    successful transcription, emits one best-effort usage-metering event.

    `model` and `temperature` are accepted for OpenAI-client compatibility and
    ignored (Parakeet picks its own model; it has no sampling temperature).
    """
    fmt = (response_format or "json").lower()
    supported = {"json", "verbose_json", "text"}
    if fmt not in supported:
        return _openai_error(
            f"response_format '{response_format}' is not supported; "
            f"use one of: {', '.join(sorted(supported))}",
            status_code=400,
            err_type="invalid_request_error",
        )

    raw = await file.read()
    if not raw:
        return _openai_error(
            "no audio file provided (field 'file' was empty)",
            status_code=400,
            err_type="invalid_request_error",
        )

    lang = language or "en"
    # Only ask Parakeet for word timestamps when the chosen format can surface
    # them (verbose_json). The minimal json/text formats don't need words, so we
    # skip the extra payload for them.
    want_words = fmt == "verbose_json"

    params = {
        "language": lang,
        "return_word_timestamps": "true" if want_words else "false",
    }
    # Multipart field is `audio` (parakeet-svc contract), NOT `file`.
    files = {"audio": (file.filename or "audio.wav", raw, file.content_type or "audio/wav")}
    timeout = _parakeet_timeout_for_bytes(len(raw))

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{PARAKEET_URL}/transcribe",
                files=files,
                params=params,
            )
    except httpx.TimeoutException:
        logger.error("parakeet timeout (%.0fs) for %s", timeout, file.filename)
        return _openai_error(
            f"transcription backend timed out after {timeout:.0f}s — the audio may be too long",
            status_code=504,
            err_type="timeout_error",
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("parakeet client error: %s", exc)
        return _openai_error(
            f"could not reach the transcription backend: {exc}",
            status_code=502,
            err_type="api_error",
        )

    if resp.status_code != 200:
        detail = (resp.text or "").strip()[:300]
        logger.error("parakeet returned %s: %s", resp.status_code, detail)
        # Surface a clean OpenAI-style error. Upstream client errors (4xx) are
        # generally caller-fixable; everything else is a bad-gateway.
        status = 400 if 400 <= resp.status_code < 500 else 502
        return _openai_error(
            f"transcription backend error {resp.status_code}: {detail}",
            status_code=status,
            err_type="api_error",
        )

    try:
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.error("parakeet returned non-JSON: %s", exc)
        return _openai_error(
            "transcription backend returned an invalid response",
            status_code=502,
            err_type="api_error",
        )

    # ---- success: shape the response for the requested format ----
    if fmt == "text":
        result: Any = PlainTextResponse((payload.get("text") or "").strip())
    elif fmt == "verbose_json":
        result = JSONResponse(content=_map_verbose_json(payload, lang))
    else:  # json
        result = JSONResponse(content=_map_json(payload))

    # ---- metering: AFTER the response is shaped, off the critical path ----
    # Attach as a Starlette BackgroundTask: it runs *after* the response body is
    # sent to the client, so a slow/unreachable ops-center can never delay STT.
    # _emit_metering already swallows all errors, so this can never break the
    # request either.
    org_id = request.headers.get(ORG_HEADER)
    audio_seconds: Optional[float] = None
    try:
        d = payload.get("duration")
        if d is not None:
            audio_seconds = float(d)
    except (TypeError, ValueError):
        audio_seconds = None

    result.background = BackgroundTask(_emit_metering, org_id, audio_seconds)
    return result


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=SHIM_PORT, reload=False)
