"""
Enhanced Agent System Models
Multi-tier agent architecture supporting core pipeline + custom agents
"""
import uuid
from datetime import datetime, timezone
from enum import Enum
from sqlalchemy import Column, String, Text, JSON, Boolean, DateTime, Integer, Float, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from database.models import Base

class AgentType(str, Enum):
    """Types of agents in the system"""
    CORE_LIVE = "core_live"           # Core pipeline - live summaries
    CORE_FINAL = "core_final"         # Core pipeline - final analysis
    TASK_TEMPLATE = "task_template"   # Task-specific templates
    CHAT_SINGLE = "chat_single"       # Chat with single meeting
    CHAT_CROSS = "chat_cross"         # Chat across meetings
    CHAT_ALL = "chat_all"             # Chat with all meetings
    EXPORT = "export"                 # Custom export formatting
    ANALYSIS = "analysis"             # Specialized analysis

class AgentPermission(str, Enum):
    """Permission levels for agents"""
    PUBLIC = "public"          # Anyone can use
    AUTHENTICATED = "authenticated"  # Any logged-in user
    TEAM = "team"             # Team members only
    ADMIN = "admin"           # Admins only
    OWNER = "owner"           # Only the creator

class MeetingAgent(Base):
    """
    Enhanced agent model supporting multiple agents with different purposes
    Inspired by Open WebUI but extended for meeting-specific needs
    """
    __tablename__ = "meeting_agents"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Agent Identity
    name = Column(String(100), nullable=False)
    slug = Column(String(100), unique=True, nullable=False)  # URL-friendly ID
    description = Column(Text)
    icon = Column(String(50), default="🤖")  # Emoji or icon class
    
    # Agent Type and Purpose
    agent_type = Column(String(50), nullable=False)  # From AgentType enum
    purpose = Column(Text)  # Detailed explanation of what this agent does
    
    # Core Pipeline Flags
    is_core = Column(Boolean, default=False)  # Protected core agent
    is_active = Column(Boolean, default=True)  # Agent enabled/disabled
    is_default_live = Column(Boolean, default=False)  # Default for live summaries
    is_default_final = Column(Boolean, default=False)  # Default for final analysis
    
    # Model Configuration
    provider_type = Column(String(50), default="ollama")  # ollama, openai, anthropic, llamacpp
    model_name = Column(String(100), nullable=False)
    model_endpoint = Column(String(200))  # Custom endpoint if needed
    model_settings = Column(JSON, default={
        "temperature": 0.7,
        "max_tokens": 1000,
        "top_p": 0.9,
        "context_window": 8192
    })
    
    # System Prompt (fully editable)
    system_prompt = Column(Text, nullable=False)
    
    # Output Configuration
    output_format = Column(String(50), default="text")  # text, json, markdown, structured
    output_template = Column(JSON)  # Expected output structure
    
    # Progressive Settings (for live agents)
    progressive_config = Column(JSON, default={
        "enabled": False,
        "initial_interval": 500,
        "multiplier": 1.0,
        "max_interval": 2000
    })
    
    # Chat Configuration (for chat agents)
    chat_config = Column(JSON, default={
        "max_history": 10,
        "include_context": True,
        "rag_enabled": False,
        "vector_search": False
    })
    
    # Permissions
    permission_level = Column(String(50), default="authenticated")
    allowed_roles = Column(JSON, default=["user", "admin"])  # Which roles can use
    allowed_users = Column(JSON, default=[])  # Specific user IDs if restricted
    
    # Ownership and Metadata
    created_by = Column(UUID(as_uuid=True))  # User who created
    organization_id = Column(UUID(as_uuid=True))  # For multi-tenant
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Import/Export Support
    version = Column(String(20), default="1.0.0")
    export_data = Column(JSON)  # Additional data for export
    source = Column(String(100))  # Where agent was imported from
    
    # Usage Statistics
    usage_count = Column(Integer, default=0)
    last_used = Column(DateTime)
    performance_metrics = Column(JSON, default={
        "avg_response_time": 0,
        "avg_tokens_per_sec": 0,
        "success_rate": 100
    })
    
    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            "id": str(self.id),
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "icon": self.icon,
            "agent_type": self.agent_type,
            "purpose": self.purpose,
            "is_core": self.is_core,
            "is_active": self.is_active,
            "is_default_live": self.is_default_live,
            "is_default_final": self.is_default_final,
            "provider_type": self.provider_type,
            "model_name": self.model_name,
            "model_endpoint": self.model_endpoint,
            "model_settings": self.model_settings,
            "system_prompt": self.system_prompt,
            "output_format": self.output_format,
            "output_template": self.output_template,
            "progressive_config": self.progressive_config,
            "chat_config": self.chat_config,
            "permission_level": self.permission_level,
            "allowed_roles": self.allowed_roles,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "usage_count": self.usage_count,
            "performance_metrics": self.performance_metrics
        }
    
    def to_export(self):
        """Export format for sharing agents"""
        return {
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "agent_type": self.agent_type,
            "purpose": self.purpose,
            "provider_type": self.provider_type,
            "model_name": self.model_name,
            "model_settings": self.model_settings,
            "system_prompt": self.system_prompt,
            "output_format": self.output_format,
            "output_template": self.output_template,
            "progressive_config": self.progressive_config,
            "chat_config": self.chat_config,
            "version": self.version,
            "export_data": self.export_data
        }
    
    @classmethod
    def from_import(cls, data: dict, created_by: uuid.UUID):
        """Create agent from imported data"""
        return cls(
            name=data.get("name"),
            slug=cls.generate_slug(data.get("name")),
            description=data.get("description"),
            icon=data.get("icon", "🤖"),
            agent_type=data.get("agent_type"),
            purpose=data.get("purpose"),
            provider_type=data.get("provider_type", "ollama"),
            model_name=data.get("model_name"),
            model_settings=data.get("model_settings", {}),
            system_prompt=data.get("system_prompt"),
            output_format=data.get("output_format", "text"),
            output_template=data.get("output_template"),
            progressive_config=data.get("progressive_config", {}),
            chat_config=data.get("chat_config", {}),
            created_by=created_by,
            version=data.get("version", "1.0.0"),
            export_data=data.get("export_data"),
            source="import"
        )
    
    @staticmethod
    def generate_slug(name: str) -> str:
        """Generate URL-friendly slug from name"""
        import re
        slug = re.sub(r'[^\w\s-]', '', name.lower())
        slug = re.sub(r'[-\s]+', '-', slug)
        return f"{slug}-{uuid.uuid4().hex[:8]}"


class AgentTemplate(Base):
    """
    Pre-built agent templates that users can instantiate
    """
    __tablename__ = "agent_templates"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Template Info
    name = Column(String(100), nullable=False)
    category = Column(String(50))  # meeting, analysis, export, chat
    description = Column(Text)
    icon = Column(String(50), default="📋")
    
    # Template Data (complete agent configuration)
    template_data = Column(JSON, nullable=False)
    
    # Metadata
    is_official = Column(Boolean, default=False)  # Official templates
    downloads = Column(Integer, default=0)
    rating = Column(Float)
    created_by = Column(UUID(as_uuid=True))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "icon": self.icon,
            "template_data": self.template_data,
            "is_official": self.is_official,
            "downloads": self.downloads,
            "rating": self.rating,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }


class AgentSession(Base):
    """
    Track agent usage in meetings for chat and interaction history
    """
    __tablename__ = "agent_sessions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Relations
    agent_id = Column(UUID(as_uuid=True), ForeignKey('meeting_agents.id'), nullable=False)
    # CASCADE on parent delete per 012_cascade_session_children.
    meeting_session_id = Column(
        Integer,
        ForeignKey('recording_sessions.id', ondelete='CASCADE'),
    )  # Optional
    user_id = Column(UUID(as_uuid=True))  # User interacting
    
    # Session Data
    session_type = Column(String(50))  # chat, analysis, export
    input_data = Column(JSON)  # What was sent to agent
    output_data = Column(JSON)  # What agent returned
    context_data = Column(JSON)  # Additional context
    
    # Performance
    tokens_used = Column(Integer, default=0)
    response_time_ms = Column(Integer)
    success = Column(Boolean, default=True)
    error_message = Column(Text)
    
    # Timestamps
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime)
    
    # Relationships
    agent = relationship("MeetingAgent", backref="sessions")