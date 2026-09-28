"""
Agent Manager Service
Manages multi-tier agent system with core pipeline + custom agents
"""
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
import uuid

from sqlalchemy.orm import Session
from database.database import get_db_session
from models.agent_system import MeetingAgent, AgentType, AgentPermission, AgentTemplate, AgentSession

logger = logging.getLogger(__name__)

class AgentManagerService:
    """
    Manages the multi-tier agent system
    """
    
    def __init__(self):
        self.core_agents = {}
        self._initialize_core_agents()
        
    def _initialize_core_agents(self):
        """Initialize or verify core pipeline agents exist"""
        with get_db_session() as db:
            # Live Summary Agent (Qwen3-0.6B ultra-fast)
            live_agent = db.query(MeetingAgent).filter_by(slug="core-live-summary").first()
            if not live_agent:
                live_agent = MeetingAgent(
                    name="Live Summary Agent",
                    slug="core-live-summary",
                    description="Ultra-fast real-time meeting summarization",
                    icon="⚡",
                    agent_type=AgentType.CORE_LIVE,
                    purpose="Provides real-time progressive summaries during meeting recording",
                    is_core=True,
                    is_active=True,
                    is_default_live=True,
                    provider_type="llamacpp",
                    model_name="qwen3-0.6b",
                    model_endpoint="http://localhost:11440",
                    model_settings={
                        "temperature": 0.7,
                        "max_tokens": 150,
                        "top_p": 0.9,
                        "context_window": 32768
                    },
                    system_prompt="""You are a fast meeting summarizer. 
Provide brief, clear summaries of the discussion.
Focus on key points, decisions, and action items.
Be concise - aim for 2-3 sentences.""",
                    output_format="text",
                    progressive_config={
                        "enabled": True,
                        "initial_interval": 500,
                        "multiplier": 1.0,
                        "max_interval": 500
                    },
                    permission_level=AgentPermission.AUTHENTICATED,
                    performance_metrics={
                        "avg_tokens_per_sec": 123.63,
                        "model_size": "0.6B",
                        "context_window": "32K"
                    }
                )
                db.add(live_agent)
                logger.info("✅ Created core Live Summary Agent (Qwen3-0.6B)")
            
            # Final Analysis Agent (Qwen3-30B or Granite)
            final_agent = db.query(MeetingAgent).filter_by(slug="core-final-analysis").first()
            if not final_agent:
                final_agent = MeetingAgent(
                    name="Final Analysis Agent",
                    slug="core-final-analysis",
                    description="Comprehensive meeting analysis with structured output",
                    icon="🎯",
                    agent_type=AgentType.CORE_FINAL,
                    purpose="Provides detailed structured analysis after meeting completion",
                    is_core=True,
                    is_active=True,
                    is_default_final=True,
                    provider_type="llamacpp",
                    model_name="qwen3-30b",
                    model_endpoint="http://localhost:11439",
                    model_settings={
                        "temperature": 0.7,
                        "max_tokens": 2000,
                        "top_p": 0.9,
                        "context_window": 32768
                    },
                    system_prompt="""You are a professional meeting analyst.
Analyze the complete meeting transcript and provide:

1. Executive Summary (2-3 sentences)
2. Key Discussion Points (3-5 bullets)
3. Action Items with owners and priorities
4. Important Decisions Made
5. Suggested Meeting Title
6. Next Steps

Format as structured JSON.""",
                    output_format="json",
                    output_template={
                        "executive": "string",
                        "bullets": ["string"],
                        "actions": [{"action": "string", "owner": "string", "priority": "string"}],
                        "decisions": ["string"],
                        "title": "string",
                        "next_steps": ["string"]
                    },
                    permission_level=AgentPermission.AUTHENTICATED,
                    performance_metrics={
                        "avg_tokens_per_sec": 26.54,
                        "model_size": "30B",
                        "context_window": "32K"
                    }
                )
                db.add(final_agent)
                logger.info("✅ Created core Final Analysis Agent (Qwen3-30B)")
            
            db.commit()
            
            # Cache core agents
            self.core_agents = {
                "live": live_agent,
                "final": final_agent
            }
    
    def get_core_agents(self) -> Dict[str, MeetingAgent]:
        """Get the core pipeline agents"""
        return self.core_agents
    
    def get_agent_by_slug(self, slug: str, db: Session) -> Optional[MeetingAgent]:
        """Get agent by slug"""
        return db.query(MeetingAgent).filter_by(slug=slug, is_active=True).first()
    
    def get_agent_by_type(self, agent_type: AgentType, db: Session) -> List[MeetingAgent]:
        """Get all agents of a specific type"""
        return db.query(MeetingAgent).filter_by(
            agent_type=agent_type,
            is_active=True
        ).all()
    
    def create_agent(self, data: Dict, created_by: uuid.UUID, db: Session) -> MeetingAgent:
        """Create a new custom agent"""
        # Generate unique slug
        slug = MeetingAgent.generate_slug(data.get("name"))
        
        agent = MeetingAgent(
            name=data.get("name"),
            slug=slug,
            description=data.get("description"),
            icon=data.get("icon", "🤖"),
            agent_type=data.get("agent_type", AgentType.TASK_TEMPLATE),
            purpose=data.get("purpose"),
            provider_type=data.get("provider_type", "ollama"),
            model_name=data.get("model_name"),
            model_endpoint=data.get("model_endpoint"),
            model_settings=data.get("model_settings", {}),
            system_prompt=data.get("system_prompt"),
            output_format=data.get("output_format", "text"),
            output_template=data.get("output_template"),
            progressive_config=data.get("progressive_config", {}),
            chat_config=data.get("chat_config", {}),
            permission_level=data.get("permission_level", AgentPermission.OWNER),
            allowed_roles=data.get("allowed_roles", ["user", "admin"]),
            created_by=created_by
        )
        
        db.add(agent)
        db.commit()
        db.refresh(agent)
        
        logger.info(f"Created new agent: {agent.name} ({agent.slug})")
        return agent
    
    def update_agent(self, slug: str, updates: Dict, db: Session) -> Optional[MeetingAgent]:
        """Update an existing agent"""
        agent = self.get_agent_by_slug(slug, db)
        if not agent:
            return None
        
        # Prevent modification of core agent types
        if agent.is_core and "agent_type" in updates:
            del updates["agent_type"]
        
        # Update allowed fields
        allowed_fields = [
            "name", "description", "icon", "purpose",
            "model_name", "model_endpoint", "model_settings",
            "system_prompt", "output_format", "output_template",
            "progressive_config", "chat_config",
            "permission_level", "allowed_roles"
        ]
        
        for field in allowed_fields:
            if field in updates:
                setattr(agent, field, updates[field])
        
        agent.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(agent)
        
        logger.info(f"Updated agent: {agent.name} ({agent.slug})")
        return agent
    
    def delete_agent(self, slug: str, db: Session) -> bool:
        """Delete an agent (cannot delete core agents)"""
        agent = self.get_agent_by_slug(slug, db)
        if not agent:
            return False
        
        if agent.is_core:
            logger.warning(f"Cannot delete core agent: {slug}")
            return False
        
        # Soft delete by deactivating
        agent.is_active = False
        agent.updated_at = datetime.now(timezone.utc)
        db.commit()
        
        logger.info(f"Deactivated agent: {agent.name} ({agent.slug})")
        return True
    
    def export_agent(self, slug: str, db: Session) -> Optional[Dict]:
        """Export agent configuration"""
        agent = self.get_agent_by_slug(slug, db)
        if not agent:
            return None
        
        return agent.to_export()
    
    def import_agent(self, data: Dict, created_by: uuid.UUID, db: Session) -> MeetingAgent:
        """Import agent from configuration"""
        agent = MeetingAgent.from_import(data, created_by)
        db.add(agent)
        db.commit()
        db.refresh(agent)
        
        logger.info(f"Imported agent: {agent.name} ({agent.slug})")
        return agent
    
    def list_agents(self, 
                   agent_type: Optional[AgentType] = None,
                   user_id: Optional[uuid.UUID] = None,
                   include_inactive: bool = False,
                   db: Session = None) -> List[MeetingAgent]:
        """List available agents based on filters"""
        query = db.query(MeetingAgent)
        
        if not include_inactive:
            query = query.filter_by(is_active=True)
        
        if agent_type:
            query = query.filter_by(agent_type=agent_type)
        
        if user_id:
            # Filter by permission level and ownership
            query = query.filter(
                (MeetingAgent.permission_level == AgentPermission.PUBLIC) |
                (MeetingAgent.created_by == user_id)
            )
        
        return query.order_by(MeetingAgent.usage_count.desc()).all()
    
    def get_default_agents(self, db: Session) -> Dict[str, MeetingAgent]:
        """Get default agents for pipeline"""
        live_agent = db.query(MeetingAgent).filter_by(is_default_live=True).first()
        final_agent = db.query(MeetingAgent).filter_by(is_default_final=True).first()
        
        return {
            "live": live_agent or self.core_agents.get("live"),
            "final": final_agent or self.core_agents.get("final")
        }
    
    def set_default_agent(self, slug: str, pipeline_role: str, db: Session) -> bool:
        """Set an agent as default for a pipeline role"""
        agent = self.get_agent_by_slug(slug, db)
        if not agent:
            return False
        
        # Clear current default
        if pipeline_role == "live":
            db.query(MeetingAgent).update({MeetingAgent.is_default_live: False})
            agent.is_default_live = True
        elif pipeline_role == "final":
            db.query(MeetingAgent).update({MeetingAgent.is_default_final: False})
            agent.is_default_final = True
        else:
            return False
        
        db.commit()
        logger.info(f"Set {agent.name} as default for {pipeline_role}")
        return True
    
    def record_usage(self, agent_id: uuid.UUID, 
                    session_data: Dict,
                    db: Session) -> AgentSession:
        """Record agent usage for analytics"""
        session = AgentSession(
            agent_id=agent_id,
            meeting_session_id=session_data.get("meeting_session_id"),
            user_id=session_data.get("user_id"),
            session_type=session_data.get("session_type"),
            input_data=session_data.get("input_data"),
            output_data=session_data.get("output_data"),
            context_data=session_data.get("context_data"),
            tokens_used=session_data.get("tokens_used", 0),
            response_time_ms=session_data.get("response_time_ms"),
            success=session_data.get("success", True),
            error_message=session_data.get("error_message"),
            completed_at=datetime.now(timezone.utc)
        )
        
        db.add(session)
        
        # Update agent usage stats
        agent = db.query(MeetingAgent).filter_by(id=agent_id).first()
        if agent:
            agent.usage_count += 1
            agent.last_used = datetime.now(timezone.utc)
            
            # Update performance metrics
            if session_data.get("response_time_ms"):
                metrics = agent.performance_metrics or {}
                current_avg = metrics.get("avg_response_time", 0)
                new_avg = (current_avg * (agent.usage_count - 1) + session_data["response_time_ms"]) / agent.usage_count
                metrics["avg_response_time"] = new_avg
                agent.performance_metrics = metrics
        
        db.commit()
        return session


# Global instance
agent_manager = AgentManagerService()