"""
Agent Management API
Full CRUD operations for multi-tier agent system
"""
from fastapi import APIRouter, HTTPException, Depends, File, UploadFile, Request
from sqlalchemy.orm import Session
from typing import List, Optional, Dict
from pydantic import BaseModel
import json
import uuid
import logging

from database.database import get_db
from services.agent_manager_service import agent_manager
from models.agent_system import AgentType, AgentPermission
from auth.dependencies import get_current_user
from auth.models import User
from auth.organization import resolve_active_organization

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["Agent Management"])

# Pydantic models for requests/responses
class AgentCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    icon: Optional[str] = "🤖"
    agent_type: str = "task_template"
    purpose: Optional[str] = None
    provider_type: str = "ollama"
    model_name: str
    model_endpoint: Optional[str] = None
    model_settings: Optional[Dict] = None
    system_prompt: str
    output_format: str = "text"
    output_template: Optional[Dict] = None
    progressive_config: Optional[Dict] = None
    chat_config: Optional[Dict] = None
    permission_level: str = "owner"
    allowed_roles: Optional[List[str]] = ["user", "admin"]

class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    purpose: Optional[str] = None
    model_name: Optional[str] = None
    model_endpoint: Optional[str] = None
    model_settings: Optional[Dict] = None
    system_prompt: Optional[str] = None
    output_format: Optional[str] = None
    output_template: Optional[Dict] = None
    progressive_config: Optional[Dict] = None
    chat_config: Optional[Dict] = None
    permission_level: Optional[str] = None
    allowed_roles: Optional[List[str]] = None

class SetDefaultRequest(BaseModel):
    agent_slug: str
    pipeline_role: str  # "live" or "final"

# CRUD Endpoints

@router.get("/")
async def list_agents(
    agent_type: Optional[str] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all available agents"""
    agents = agent_manager.list_agents(
        agent_type=AgentType(agent_type) if agent_type else None,
        include_inactive=include_inactive,
        db=db
    )
    return {
        "agents": [agent.to_dict() for agent in agents],
        "count": len(agents)
    }

@router.get("/core")
async def get_core_agents(current_user: User = Depends(get_current_user)):
    """Get the core pipeline agents"""
    core_agents = agent_manager.get_core_agents()
    return {
        "live": core_agents["live"].to_dict() if core_agents.get("live") else None,
        "final": core_agents["final"].to_dict() if core_agents.get("final") else None
    }

@router.get("/defaults")
async def get_default_agents(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Get current default agents for pipeline"""
    defaults = agent_manager.get_default_agents(db)
    return {
        "live": defaults["live"].to_dict() if defaults.get("live") else None,
        "final": defaults["final"].to_dict() if defaults.get("final") else None
    }

@router.post("/defaults")
async def set_default_agent(
    request: SetDefaultRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Set an agent as default for pipeline role"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser required")
    success = agent_manager.set_default_agent(
        request.agent_slug,
        request.pipeline_role,
        db
    )
    
    if not success:
        raise HTTPException(status_code=400, detail="Failed to set default agent")
    
    return {"success": True, "message": f"Set {request.agent_slug} as default for {request.pipeline_role}"}

@router.get("/{slug}")
async def get_agent(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get specific agent by slug.

    First checks the DB-backed agent registry (core-live-summary,
    core-final-analysis, user-imported templates). If no row matches we
    fall back to the federated source registry — that's how local agents
    like ``meeting-rag`` (which has no DB row by design) surface here so
    the frontend can fetch their metadata at ``/api/agents/<slug>``."""
    agent = agent_manager.get_agent_by_slug(slug, db)
    if agent:
        return agent.to_dict()

    try:
        from services.agents import get_agent_registry

        # Federated descriptors are org-aware. Resolve the active org if we
        # have an authenticated user (header-forwarded auth); otherwise 0
        # gets us a metadata-only lookup, which is what most callers want
        # from this endpoint.
        org_id = 0
        if current_user is not None:
            try:
                active_org = resolve_active_organization(db, request, current_user)
                org_id = active_org.organization.id
            except Exception:
                org_id = 0

        registry = get_agent_registry()
        descriptor = await registry.get_agent(slug, request, db, org_id)
        if descriptor:
            return descriptor.to_dict()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Federated agent lookup failed for slug={slug}: {exc}")

    raise HTTPException(status_code=404, detail="Agent not found")

@router.post("/")
async def create_agent(
    request: AgentCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new custom agent"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser required")

    # created_by is a UUID column; User.id is an int. Map deterministically
    # (same user -> same UUID, reversible via .int) so ownership filters hold.
    user_id = uuid.UUID(int=current_user.id)

    agent = agent_manager.create_agent(
        request.dict(),
        created_by=user_id,
        db=db
    )
    
    return {
        "success": True,
        "agent": agent.to_dict(),
        "message": f"Created agent: {agent.name}"
    }

@router.put("/{slug}")
async def update_agent(
    slug: str,
    request: AgentUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update an existing agent"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser required")
    agent = agent_manager.update_agent(
        slug,
        request.dict(exclude_unset=True),
        db
    )
    
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    return {
        "success": True,
        "agent": agent.to_dict(),
        "message": f"Updated agent: {agent.name}"
    }

@router.delete("/{slug}")
async def delete_agent(slug: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Delete (deactivate) an agent"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser required")
    success = agent_manager.delete_agent(slug, db)
    
    if not success:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete agent (may be core agent or not found)"
        )
    
    return {"success": True, "message": f"Deactivated agent: {slug}"}

# Import/Export Endpoints

@router.get("/{slug}/export")
async def export_agent(slug: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Export agent configuration"""
    config = agent_manager.export_agent(slug, db)
    
    if not config:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    return config

@router.post("/import")
async def import_agent(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Import agent from JSON file"""
    try:
        if not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="Superuser required")
        content = await file.read()
        data = json.loads(content)

        # created_by is a UUID column; User.id is an int. Map deterministically.
        user_id = uuid.UUID(int=current_user.id)

        agent = agent_manager.import_agent(data, user_id, db)
        
        return {
            "success": True,
            "agent": agent.to_dict(),
            "message": f"Imported agent: {agent.name}"
        }
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON file")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# Template Endpoints

@router.get("/templates/")
async def list_templates(
    category: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List available agent templates"""
    from models.agent_system import AgentTemplate
    
    query = db.query(AgentTemplate)
    if category:
        query = query.filter_by(category=category)
    
    templates = query.order_by(AgentTemplate.downloads.desc()).all()
    
    return {
        "templates": [t.to_dict() for t in templates],
        "count": len(templates)
    }

@router.post("/templates/{template_id}/instantiate")
async def instantiate_template(
    template_id: str,
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create agent from template"""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Superuser required")
    from models.agent_system import AgentTemplate
    
    template = db.query(AgentTemplate).filter_by(id=template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template not found")
    
    # Create agent from template data
    agent_data = template.template_data.copy()
    agent_data["name"] = name

    # created_by is a UUID column; User.id is an int. Map deterministically.
    user_id = uuid.UUID(int=current_user.id)

    agent = agent_manager.create_agent(agent_data, user_id, db)
    
    # Increment template downloads
    template.downloads += 1
    db.commit()
    
    return {
        "success": True,
        "agent": agent.to_dict(),
        "message": f"Created agent from template: {agent.name}"
    }

# Agent Type Information

@router.get("/types/")
async def get_agent_types(current_user: User = Depends(get_current_user)):
    """Get available agent types and their descriptions"""
    return {
        "types": [
            {
                "value": AgentType.CORE_LIVE,
                "label": "Core Live Summary",
                "description": "Real-time progressive summaries during recording",
                "icon": "⚡"
            },
            {
                "value": AgentType.CORE_FINAL,
                "label": "Core Final Analysis",
                "description": "Comprehensive analysis after recording",
                "icon": "🎯"
            },
            {
                "value": AgentType.TASK_TEMPLATE,
                "label": "Task Template",
                "description": "Custom templates for specific meeting types",
                "icon": "📋"
            },
            {
                "value": AgentType.CHAT_SINGLE,
                "label": "Single Meeting Chat",
                "description": "Chat interface for single meeting context",
                "icon": "💬"
            },
            {
                "value": AgentType.CHAT_CROSS,
                "label": "Cross-Meeting Chat",
                "description": "Chat across multiple meetings",
                "icon": "🔄"
            },
            {
                "value": AgentType.CHAT_ALL,
                "label": "All Meetings Chat",
                "description": "Chat with entire meeting database",
                "icon": "🌐"
            },
            {
                "value": AgentType.EXPORT,
                "label": "Export Formatter",
                "description": "Custom export formatting",
                "icon": "📤"
            },
            {
                "value": AgentType.ANALYSIS,
                "label": "Specialized Analysis",
                "description": "Domain-specific analysis (legal, medical, etc)",
                "icon": "🔬"
            }
        ]
    }

# Model Performance Information

@router.get("/models/performance")
async def get_model_performance(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get performance metrics for the available models.

    Reports availability of the active org's configured LLM provider via the
    ProviderRegistry. When no caller org context can be resolved, falls back
    to the system default LiteLLM provider.
    """
    from services.providers import get_provider_registry
    registry = get_provider_registry(db)

    org_id = None
    org_slug = None
    if current_user is not None:
        try:
            active_org = resolve_active_organization(db, request, current_user)
            org_id = active_org.organization.id
            org_slug = active_org.organization.slug
        except Exception as exc:
            logger.debug(f"Per-org context resolution failed: {exc}")

    try:
        provider = registry.get_llm(org_id=org_id, task="quality") if org_id else registry.default_llm()
        health = await provider.health()
        llm_available = bool(health.get("available"))
        per_org_provider = {
            "org_slug": org_slug,
            "endpoint": health.get("endpoint", ""),
            "model": health.get("model", ""),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.debug(f"Provider health probe failed: {exc}")
        llm_available = False
        per_org_provider = None

    base_endpoint = (per_org_provider or {}).get("endpoint") or "http://unicorn-litellm:4000/v1"
    base_model_name = (per_org_provider or {}).get("model") or "gemma-4-26b-moe"

    return {
        "models": [
            {
                "name": base_model_name,
                "label": "Active LLM",
                "tokens_per_second": 850,
                "context_window": 8192,
                "vram_usage": "~1.5GB",
                "best_for": "Summaries, chat, insights, sentiment analysis",
                "endpoint": base_endpoint,
                "backend": "llama.cpp Vulkan",
                "status": "active" if llm_available else "unavailable",
            }
        ],
        "per_org_provider": per_org_provider,
    }
