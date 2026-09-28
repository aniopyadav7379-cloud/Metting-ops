"""
Agent Configuration Models
Inspired by Open WebUI's custom models approach
"""
from sqlalchemy import Column, String, Text, JSON, DateTime, Boolean, Integer, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from uuid import uuid4
try:
    from backend.database import Base
except ImportError:
    from database import Base

class AgentConfiguration(Base):
    """Agent configuration decoupled from model settings"""
    __tablename__ = "agent_configurations"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    name = Column(String(100), nullable=False)
    description = Column(Text)
    agent_type = Column(String(50), nullable=False)  # 'live_enhancement' or 'final_analysis'
    
    # Core agent behavior (what the agent does, not how it's powered)
    system_prompt = Column(Text, nullable=False)
    tools = Column(JSON, default=list)  # List of tool names/configs
    custom_instructions = Column(Text)
    
    # Model selection - reference to separate model configuration
    model_config_id = Column(String, ForeignKey('model_configurations.id'), nullable=True)
    
    # Optional: Direct model specification for backwards compatibility
    # These are used only if model_config_id is not set
    fallback_provider = Column(String(50))  # 'ollama', 'openai', 'anthropic'
    fallback_model_name = Column(String(100))  # e.g., 'granite3.3:8b'
    
    # Agent-specific parameter overrides
    # These override the model's default parameters when this agent uses the model
    parameter_overrides = Column(JSON, default=dict)
    
    # Pipeline configuration for multi-model agents
    pipeline_config = Column(JSON, default=dict)  # e.g., {"early_model_id": "...", "late_model_id": "..."}
    
    # Update strategy for live agents (behavioral, not model-specific)
    update_interval_min = Column(Integer, default=30)  # Minimum seconds between updates
    update_interval_max = Column(Integer, default=90)  # Maximum seconds between updates
    min_words_trigger = Column(Integer, default=150)  # Minimum words to trigger update
    silence_trigger_seconds = Column(Integer, default=10)  # Silence duration to trigger
    priority_keywords = Column(JSON, default=list)  # Keywords that trigger immediate update
    
    # Metadata
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    created_by = Column(Integer, ForeignKey('users.id'))
    
    # Relationships
    creator = relationship("User", backref="agent_configs")


class AgentProvider(Base):
    """Third-party API providers configuration"""
    __tablename__ = "agent_providers"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    name = Column(String(100), nullable=False, unique=True)
    provider_type = Column(String(50), nullable=False)  # 'ollama', 'openai_compatible', 'custom'
    api_url = Column(String(255), nullable=False)
    api_key = Column(String(255))  # Encrypted in production
    
    # Available models from this provider
    available_models = Column(JSON, default=list)
    
    # Connection settings
    timeout_seconds = Column(Integer, default=120)
    max_retries = Column(Integer, default=3)
    
    # Metadata
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    last_health_check = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class AgentExecution(Base):
    """Track agent executions for optimization and debugging"""
    __tablename__ = "agent_executions"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    agent_id = Column(String, ForeignKey('agent_configurations.id'))
    session_id = Column(Integer, ForeignKey('recording_sessions.id'))
    
    # Execution details
    execution_type = Column(String(50))  # 'scheduled', 'triggered', 'manual'
    trigger_reason = Column(String(255))  # Why was this execution triggered
    
    # Input/Output
    input_text = Column(Text)
    input_word_count = Column(Integer)
    output_content = Column(JSON)  # Structured output (notes, summary, etc.)
    
    # Performance metrics
    execution_time_ms = Column(Integer)
    tokens_used = Column(Integer)
    model_used = Column(String(100))
    
    # Status
    status = Column(String(50))  # 'success', 'failed', 'timeout'
    error_message = Column(Text)
    
    # Timestamps
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True))
    
    # Relationships
    agent = relationship("AgentConfiguration", backref="executions")
    session = relationship("RecordingSession", backref="agent_executions")