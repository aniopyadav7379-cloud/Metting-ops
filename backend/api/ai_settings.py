"""
AI Settings API endpoints for managing LLM and transcription configurations.

Reports the currently configured LLM tier in two layers:
  1. Per-org configuration resolved via the ProviderRegistry (when an active
     organization can be determined for the request).
  2. System-wide LLM provider health via ProviderRegistry
     singleton, used as a fallback when no org context is available.
"""
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
import os
import json
import logging

from database.database import get_db
from auth.dependencies import get_current_user
from auth.models import User
from auth.organization import resolve_active_organization

router = APIRouter(prefix="/api", tags=["ai-settings"])
logger = logging.getLogger(__name__)


def _per_org_llm_overlay(
    request: Request,
    current_user: Optional[User],
    db: Session,
) -> Optional[Dict[str, Any]]:
    """Resolve the per-org LLM provider configuration for this caller via the
    ProviderRegistry. Returns None when no org context is available."""
    if current_user is None:
        return None
    try:
        active_org = resolve_active_organization(db, request, current_user)
    except Exception:
        return None
    try:
        from services.providers import get_provider_registry

        registry = get_provider_registry(db)
        provider = registry.get_llm(org_id=active_org.organization.id, task="quality")
        return {
            "org_slug": active_org.organization.slug,
            "endpoint": getattr(provider, "endpoint", ""),
            "model": getattr(provider, "model", ""),
            "task": "quality",
        }
    except Exception as exc:
        logger.debug(f"Per-org LLM overlay failed: {exc}")
        return None


class ModelSwitchRequest(BaseModel):
    model_id: str


@router.get("/settings/models")
async def get_model_info(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return actual running model configuration from the model registry,
    overlayed with the active org's per-org LLM provider configuration when
    one can be resolved."""
    from config.model_registry import MODEL_REGISTRY, get_active_model

    active = get_active_model()

    # LLM health resolved from the active org's configured provider via
    # ProviderRegistry. Falls through to the registry default when no caller
    # org context is available.
    llm_info: Dict[str, Any] = {"llamacpp_available": False, "base_url": "", "model": active["model_id"]}
    try:
        from services.providers import get_provider_registry
        registry = get_provider_registry(db)
        org_id = None
        if current_user is not None:
            from auth.organization import resolve_active_organization
            try:
                ao = resolve_active_organization(db, request, current_user)
                org_id = ao.organization.id
            except Exception:
                pass
        provider = registry.get_llm(org_id=org_id, task="quality") if org_id else registry.default_llm()
        health = await provider.health()
        llm_info = {
            "llamacpp_available": bool(health.get("available")),
            "base_url": health.get("endpoint", ""),
            "model": health.get("model", active["model_id"]),
        }
        if health.get("error"):
            llm_info["error"] = health["error"]
    except Exception as e:
        llm_info["error"] = str(e)

    org_llm = _per_org_llm_overlay(request, current_user, db)

    # Get whisper-server (Vulkan iGPU) status
    whisper_server_info: Dict[str, Any] = {"available": False}
    try:
        from services.whisper_server_client import whisper_server_client
        ws_available = whisper_server_client.is_available()
        whisper_server_info = {
            "available": ws_available,
            "model": "large-v3-turbo",
            "backend": "whisper.cpp Vulkan (780M iGPU)",
            "endpoint": whisper_server_client.base_url,
            "speed": "~9.6x realtime",
            "rtf": 0.104,
        }
    except Exception as e:
        whisper_server_info["error"] = str(e)

    # Get fallback STT model info from transcription_service
    stt_fallback: Dict[str, Any] = {}
    try:
        from services.transcription_service import transcription_service
        stt_fallback = transcription_service.get_current_model_info()
    except Exception as e:
        stt_fallback = {"error": str(e)}

    return {
        "llm": {
            "model": (org_llm and org_llm.get("model")) or active["model_id"],
            "model_label": active["display_name"],
            "endpoint": (org_llm and org_llm.get("endpoint")) or llm_info.get("base_url", "http://localhost:11437"),
            "available": llm_info.get("llamacpp_available", False),
            "active_engine": llm_info.get("active_engine", "none"),
            "backend": "llama.cpp Vulkan",
            "speed_estimate": active.get("speed_estimate", "unknown"),
            "context_window": active.get("ctx_size", 8192),
            "vram_usage": active.get("vram_estimate", "unknown"),
            "max_tokens_multiplier": active.get("max_tokens_multiplier", 1.0),
            "per_org_provider": org_llm,
        },
        "stt": {
            "primary": whisper_server_info,
            "fallback": stt_fallback,
            "active": "whisper-server-vulkan" if whisper_server_info.get("available") else "cpu-fallback",
        },
        "available_llm_models": [
            {
                "name": model_id,
                "label": entry["display_name"],
                "speed_estimate": entry.get("speed_estimate", "unknown"),
                "context_window": entry.get("ctx_size", 8192),
                "vram_usage": entry.get("vram_estimate", "unknown"),
                "best_for": entry.get("best_for", ""),
                "max_tokens_multiplier": entry.get("max_tokens_multiplier", 1.0),
                "endpoint": "http://localhost:11437",
                "status": "active" if model_id == active["model_id"] else "available",
            }
            for model_id, entry in MODEL_REGISTRY.items()
        ],
    }


@router.get("/settings/models/available")
async def get_available_models(current_user: User = Depends(get_current_user)):
    """Return list of models from the registry with active indicator"""
    from config.model_registry import MODEL_REGISTRY, get_active_model

    active = get_active_model()
    models = []
    for model_id, entry in MODEL_REGISTRY.items():
        models.append({
            "model_id": model_id,
            "display_name": entry["display_name"],
            "gguf_file": entry.get("gguf_file", ""),
            "speed_estimate": entry.get("speed_estimate", "unknown"),
            "vram_estimate": entry.get("vram_estimate", "unknown"),
            "ctx_size": entry.get("ctx_size", 8192),
            "best_for": entry.get("best_for", ""),
            "max_tokens_multiplier": entry.get("max_tokens_multiplier", 1.0),
            "is_active": model_id == active["model_id"],
        })
    return {"models": models, "active_model": active["model_id"]}


@router.post("/settings/models/active")
async def set_active_model(request: ModelSwitchRequest, current_user: User = Depends(get_current_user)):
    """Set the active LLM model via settings_manager.

    Note: Switching the loaded model requires restarting the llama-gpu
    container with a different GGUF file. This endpoint updates the
    persisted setting; the container must be restarted separately.
    """
    from config.model_registry import MODEL_REGISTRY
    from services.settings_manager import settings_manager

    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser required")

    if request.model_id not in MODEL_REGISTRY:
        raise HTTPException(status_code=400, detail=f"Unknown model: {request.model_id}. Available: {list(MODEL_REGISTRY.keys())}")

    success = settings_manager.update_setting("ai.llm_model", request.model_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update model setting")

    entry = MODEL_REGISTRY[request.model_id]
    logger.info(f"Active LLM model switched to: {request.model_id} ({entry['display_name']})")

    return {
        "status": "success",
        "active_model": request.model_id,
        "display_name": entry["display_name"],
        "message": f"Model set to {entry['display_name']}. If the llama.cpp container is running a different GGUF, restart it to load {entry['gguf_file']}.",
    }


@router.get("/settings/ai")
async def get_ai_settings(current_user: User = Depends(get_current_user)):
    """Get current AI settings from the centralized settings manager"""
    try:
        from services.settings_manager import settings_manager
        all_settings = settings_manager.get_all_settings()
        return all_settings.get("ai", {})
    except Exception as e:
        logger.warning(f"Could not load AI settings: {e}")
        return {}


@router.put("/settings/ai")
async def update_ai_settings(settings: Dict[str, Any], current_user: User = Depends(get_current_user)):
    """Update AI settings"""
    try:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser required")
        from services.settings_manager import settings_manager
        success = settings_manager.update_category_settings("ai", settings)
        if success:
            return {"status": "success", "message": "AI settings updated"}
        raise HTTPException(status_code=500, detail="Failed to update settings")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
