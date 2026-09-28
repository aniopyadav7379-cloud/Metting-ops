"""System capabilities + live pipeline status.

Two endpoints:
- GET /api/system/capabilities — static host flags (build = cloud / appliance)
- GET /api/system/pipeline    — live, probes each backend for its actual
  configured model + endpoint + reachable status. Used by the
  "Agent & Pipeline Status" panel and anywhere the UI wants to truthfully
  surface what is wired up vs hardcoded labels.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api/system", tags=["System"])


def _local_audio_disabled() -> bool:
    val = os.getenv("DISABLE_LOCAL_AUDIO", "").strip().lower()
    return val in ("1", "true", "yes", "on")


@router.get("/capabilities")
async def get_capabilities() -> dict:
    """Static host capability flags (no probes)."""
    local_audio = not _local_audio_disabled()
    return {
        "local_audio": local_audio,
        "npu": local_audio and os.path.exists("/dev/accel/accel0"),
        "build": "appliance" if local_audio else "cloud",
    }


async def _probe_json(url: str, timeout: float = 3.0) -> Optional[dict]:
    """GET a /health-shaped URL and return parsed JSON or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return None
            try:
                return resp.json()
            except ValueError:
                return None
    except Exception:
        return None


@router.get("/pipeline")
async def get_pipeline_status() -> dict[str, Any]:
    """Live snapshot of the configured inference pipeline.

    Reports the actual STT engine, diarization backend, and LLM model the
    backend is wired to RIGHT NOW. Used by the Live Recording status panel
    to render real labels instead of UC-1 legacy hardcodes.

    No auth required — status info only, no secrets in the response.
    """
    parakeet_url = os.getenv("PARAKEET_SERVER_URL", "http://meet-parakeet-svc:8881")
    # WHISPER_SERVER_URL is empty by default since 2026-05-19 — only probe if
    # an org / deploy explicitly set one so we don't burn 3s on every
    # capability check waiting for a container that no longer exists.
    whisper_url = os.getenv("WHISPER_SERVER_URL", "").strip()
    speaker_url = os.getenv("SPEAKER_SVC_URL", "http://meet-speaker-svc:8889")

    parakeet_health = await _probe_json(f"{parakeet_url.rstrip('/')}/health")
    whisper_health = (
        await _probe_json(f"{whisper_url.rstrip('/')}/health")
        if whisper_url
        else None
    )
    speaker_health = await _probe_json(f"{speaker_url.rstrip('/')}/health")

    stt_default = os.getenv("STT_DEFAULT_PROVIDER", "parakeet").lower()
    stt_active = "parakeet" if stt_default in ("parakeet", "nemo", "parakeet-tdt") else "whisper"
    if stt_active == "parakeet" and not parakeet_health and whisper_health:
        stt_active = "whisper"

    if stt_active == "parakeet":
        stt_block = {
            "engine": "parakeet",
            "model": (parakeet_health or {}).get("model", "nvidia/parakeet-tdt-1.1b"),
            "endpoint": parakeet_url,
            "ready": bool((parakeet_health or {}).get("model_loaded")),
            "gpu": (parakeet_health or {}).get("gpu_name"),
            "label": "NeMo Parakeet-TDT 1.1B",
        }
    else:
        stt_block = {
            "engine": "whisper",
            "model": "whisper-large-v3-turbo",
            "endpoint": whisper_url or "(WHISPER_SERVER_URL unset)",
            "ready": bool(whisper_health),
            "gpu": None,
            "label": "whisper.cpp large-v3-turbo",
        }

    pyannote_ok = bool((speaker_health or {}).get("diarizer_available"))
    diarization_block = {
        "backend": "pyannote-3.1" if pyannote_ok else "ecapa-cluster-fallback",
        "endpoint": speaker_url,
        "ready": bool(speaker_health),
        "gpu": (speaker_health or {}).get("gpu_name"),
        "label": (
            "Pyannote 3.1 (multi-speaker)"
            if pyannote_ok
            else "ECAPA cluster (single-speaker fallback)"
        ),
        "hf_token_required": not pyannote_ok,
    }

    direct_url = os.getenv("MEETING_OPS_SUMMARIZER_URL", "").strip()
    direct_model = os.getenv("MEETING_OPS_SUMMARIZER_MODEL", "").strip()
    if direct_url and direct_model:
        llm_health = await _probe_json(direct_url.rstrip("/") + "/models")
        llm_block = {
            "route": "direct",
            "model": direct_model,
            "endpoint": direct_url,
            "ready": bool(llm_health),
            "label": direct_model,
            "thinking": False,
        }
    else:
        litellm_url = os.getenv("OPENAI_API_BASE", "http://unicorn-litellm:4000/v1")
        model = os.getenv("LLM_MODEL_QUALITY", "gemma-4-26b-moe")
        llm_block = {
            "route": "litellm",
            "model": model,
            "endpoint": litellm_url,
            "ready": True,
            "label": model,
            "thinking": False,
        }

    # Build a list of LLM models the caller can actually pick. Pulls from
    # the direct summarizer URL (if set) AND the LiteLLM gateway, filters
    # out external proprietary models (claude/gpt-4*/gemini/etc) that cost
    # money + don't run on our hardware, and marks the active default so
    # the UI dropdown can label it.
    available_models = await _build_available_models(llm_block)

    return {
        "stt": stt_block,
        "diarization": diarization_block,
        "llm": llm_block,
        "available_llm_models": available_models,
        "build": "appliance" if not _local_audio_disabled() else "cloud",
    }


# Model ids served by external proprietary APIs in the LiteLLM gateway —
# they are reachable but cost money per call and aren't running on our GPUs,
# so we hide them from the default per-upload picker. Caller can still pick
# them by typing the id into the api directly. The filter is intentionally
# liberal on locals: anything not on this list shows up.
_EXTERNAL_MODEL_MARKERS = (
    # OpenAI / Anthropic / Google paid APIs
    "gpt-4", "gpt-3.5", "claude-", "gemini-",
    # Meta llamas routed externally (none of llama-3.x is loaded locally on
    # our GPUs today; if that changes, drop the matching prefix here)
    "llama-3.1", "llama-3.2", "llama-3.3", "llama-3-",
    # Other external proprietary or partner-hosted models
    "deepseek-", "mistral-", "ministral-", "codestral", "pixtral-",
    "nova-", "grok-", "hermes-", "command-r", "perplexity-", "inflection-",
    "phi-3.5", "gemma-2-9b", "zephyr-",
    # Image / generative routes — not for summarization
    "dall-e-", "stable-diffusion", "flux-",
    # Older Qwen variants we don't host locally
    "qwq-", "qwen-2.5-", "qvq-",
    # Bring-your-own-key entries are proxy-only
    "byok",
)


def _is_local_model_id(model_id: str) -> bool:
    """Heuristic for 'this model is hosted on our hardware vs an external paid API'."""
    s = (model_id or "").lower()
    if not s:
        return False
    return not any(marker in s for marker in _EXTERNAL_MODEL_MARKERS)


async def _build_available_models(llm_block: dict) -> list[dict]:
    """Return [{id, label, route, ready, default}, ...] for the UI dropdown.

    Sources:
    - Direct summarizer endpoint (if MEETING_OPS_SUMMARIZER_URL is set) —
      lists models served by that gateway. Single Qwen3.6 today on midboy1.
    - LiteLLM gateway /v1/models — the larger catalog of local-tier models
      plus external proprietary APIs. We filter out the external ones via
      `_is_local_model_id`.

    The current configured default (llm_block.model) is flagged with
    `default: True`; if it isn't present in the upstream list we still
    include it as the first entry so the dropdown can render it.
    """
    seen: dict[str, dict] = {}

    # Direct summarizer first — Qwen3.6 today, prefer to surface as preferred.
    direct_url = os.getenv("MEETING_OPS_SUMMARIZER_URL", "").strip()
    if direct_url:
        payload = await _probe_json(direct_url.rstrip("/") + "/models")
        for m in (payload or {}).get("data", []) or (payload or {}).get("models", []) or []:
            mid = m.get("id") or m.get("name") or m.get("model")
            if not mid:
                continue
            seen[mid] = {
                "id": mid,
                "label": mid,
                "route": "direct",
                "ready": True,
                "default": False,
            }

    # LiteLLM gateway catalog
    litellm_base = os.getenv("OPENAI_API_BASE", "http://unicorn-litellm:4000/v1")
    api_key = os.getenv("OPENAI_API_KEY") or os.getenv("LITELLM_API_KEY") or ""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(litellm_base.rstrip("/") + "/models", headers=headers)
            if resp.status_code == 200:
                payload = resp.json()
                for m in payload.get("data", []) or []:
                    mid = m.get("id") or m.get("name")
                    if not mid or mid in seen:
                        continue
                    if not _is_local_model_id(mid):
                        continue
                    seen[mid] = {
                        "id": mid,
                        "label": mid,
                        "route": "litellm",
                        "ready": True,
                        "default": False,
                    }
    except Exception:
        pass

    # Make sure the actual default is in the list and flagged.
    default_id = llm_block.get("model")
    if default_id:
        if default_id not in seen:
            seen[default_id] = {
                "id": default_id,
                "label": default_id,
                "route": llm_block.get("route", "litellm"),
                "ready": bool(llm_block.get("ready")),
                "default": True,
            }
        else:
            seen[default_id]["default"] = True

    # Stable ordering: default first, then alphabetical by id.
    ordered = sorted(
        seen.values(),
        key=lambda m: (0 if m.get("default") else 1, m["id"].lower()),
    )
    return ordered
