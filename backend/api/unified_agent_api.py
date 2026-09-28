"""
Unified Agent API
Simple endpoints for single meeting agent management
"""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from typing import Dict, Optional
from pydantic import BaseModel

from database.database import get_db
from auth.dependencies import get_current_user
from auth.models import User
from models.unified_agent import UnifiedMeetingAgent
from services.unified_agent_service import unified_agent_service

router = APIRouter(prefix="/api/unified-agent", tags=["Unified Agent"])

# Pydantic models
class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    personality: Optional[str] = None
    system_prompt: Optional[str] = None
    provider_type: Optional[str] = None
    model_name: Optional[str] = None
    model_settings: Optional[Dict] = None  # Renamed from model_config
    progressive_config: Optional[Dict] = None
    output_template: Optional[Dict] = None

@router.get("/status")
async def get_agent_status(current_user: User = Depends(get_current_user)):
    """Get current unified agent status and configuration"""
    return unified_agent_service.get_agent_status()

@router.get("/agent")
async def get_agent(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Get the current unified agent configuration"""
    agent = UnifiedMeetingAgent.get_default_agent(db)
    
    if not agent:
        # Create default agent if none exists
        agent = UnifiedMeetingAgent.create_default_agent(db)
    
    return agent.to_dict()

@router.put("/agent")
async def update_agent(
    update_request: AgentUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update the unified agent configuration"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser required")
    agent = UnifiedMeetingAgent.get_default_agent(db)
    
    if not agent:
        agent = UnifiedMeetingAgent.create_default_agent(db)
    
    # Update fields that were provided
    update_data = update_request.dict(exclude_unset=True)
    
    # model_settings is already the correct field name now
    for field, value in update_data.items():
        if hasattr(agent, field):
            setattr(agent, field, value)
    
    # Update timestamp
    agent.updated_at = datetime.now(timezone.utc)
    
    db.commit()
    db.refresh(agent)
    
    return {
        "success": True,
        "message": "Agent updated successfully",
        "agent": agent.to_dict()
    }

@router.post("/agent/reset")
async def reset_agent(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Reset agent to default configuration"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser required")
    # Delete current agent
    current_agent = UnifiedMeetingAgent.get_default_agent(db)
    if current_agent:
        db.delete(current_agent)
        db.commit()
    
    # Create new default agent
    new_agent = UnifiedMeetingAgent.create_default_agent(db)
    
    return {
        "success": True,
        "message": "Agent reset to default configuration",
        "agent": new_agent.to_dict()
    }

@router.get("/models")
async def get_available_models(current_user: User = Depends(get_current_user)):
    """Get available models for the agent"""
    try:
        import requests
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        
        if response.status_code == 200:
            ollama_models = response.json().get('models', [])
            
            # Filter to supported models
            supported_models = []
            for model in ollama_models:
                model_name = model.get('name', '')
                if any(supported in model_name for supported in ['granite3.3', 'granite', 'phi4', 'gemma3n']):
                    supported_models.append({
                        "name": model_name,
                        "size": model.get('size', 0),
                        "modified_at": model.get('modified_at', ''),
                        "recommended": "granite3.3" in model_name
                    })
            
            return {
                "available": True,
                "models": supported_models,
                "default": "granite3.3:8b"
            }
        else:
            return {
                "available": False,
                "error": "Ollama not responding",
                "models": [],
                "default": "granite3.3:8b"
            }
            
    except Exception as e:
        return {
            "available": False,
            "error": str(e),
            "models": [],
            "default": "granite3.3:8b"
        }

@router.get("/providers")
async def get_available_providers(current_user: User = Depends(get_current_user)):
    """Report the LLM route this deployment is actually configured to use.

    Derived from the same env resolution ProviderRegistry.get_llm performs
    (MEETING_OPS_LLM_* host-level direct route, else the LiteLLM gateway +
    LLM_MODEL_{FAST,QUALITY,CHAT}), mirroring api/system_caps.py — no
    hardcoded catalog and no availability claims this endpoint hasn't
    verified. Per-org Provider Settings (Settings -> AI Providers) still
    override this resolution at call time.
    """
    # Host-level direct route, with the legacy summarizer-specific envs as
    # fallback (same precedence as ProviderRegistry.get_llm layer 3).
    direct_url = (
        os.getenv("MEETING_OPS_LLM_URL", "").strip()
        or os.getenv("MEETING_OPS_SUMMARIZER_URL", "").strip()
    )
    direct_model = (
        os.getenv("MEETING_OPS_LLM_MODEL", "").strip()
        or os.getenv("MEETING_OPS_SUMMARIZER_MODEL", "").strip()
    )
    use_direct = bool(direct_url and direct_model)

    # Per-task models on the gateway route — same env keys + code default
    # as ProviderRegistry (Qwen 3.6 35B-A3B is the consolidated default).
    task_models = {
        task: os.getenv(env_key, "Qwen3.6-35B-A3B-Vision")
        for task, env_key in (
            ("fast", "LLM_MODEL_FAST"),
            ("quality", "LLM_MODEL_QUALITY"),
            ("chat", "LLM_MODEL_CHAT"),
        )
    }

    providers = []
    if use_direct:
        providers.append({
            "type": "direct",
            "name": "Direct LLM route",
            "endpoint": direct_url,
            "models": {task: direct_model for task in task_models},
            "active": True,
        })
    providers.append({
        "type": "litellm",
        "name": "LiteLLM gateway",
        "endpoint": os.getenv("OPENAI_API_BASE", "http://unicorn-litellm:4000/v1"),
        "models": task_models,
        "active": not use_direct,
    })

    return {
        "providers": providers,
        "default": providers[0]["type"],
        "note": (
            "Resolved from live deployment config; per-org Provider Settings "
            "override at call time. Live reachability is reported by "
            "GET /api/system/pipeline."
        ),
    }
