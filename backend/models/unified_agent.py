"""
Unified Meeting Agent Model
Simplified single-agent approach inspired by Open WebUI
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Text, JSON, Boolean, DateTime
from sqlalchemy.dialects.postgresql import UUID
from database.models import Base

class UnifiedMeetingAgent(Base):
    """
    Single editable meeting agent (like Open WebUI custom models)
    Handles all meeting tasks: transcription analysis, summarization, action items, etc.
    """
    __tablename__ = "unified_meeting_agents"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Basic Info (editable)
    name = Column(String(100), nullable=False, default="Meeting Assistant")
    description = Column(Text, default="AI assistant for meeting transcription and analysis")
    personality = Column(String(200), default="Professional and concise")
    
    # System Prompt (fully editable like Open WebUI)
    system_prompt = Column(Text, default="""You are a professional meeting assistant. 
Analyze meeting transcripts and provide structured summaries.

Generate comprehensive analysis including:
- Executive summary (2-3 sentences)
- Key bullet points (3-5 main topics)
- Action items with owners when mentioned
- Important decisions made
- Suggested meeting title

Be concise, accurate, and focus on actionable insights.""")
    
    # Model Configuration
    provider_type = Column(String(50), default="ollama")  # ollama, openai, anthropic
    model_name = Column(String(100), default="granite3.3:8b")
    model_settings = Column(JSON, default={
        "temperature": 0.7,
        "max_tokens": 1000,
        "top_p": 0.9
    })
    
    # Progressive Settings
    progressive_config = Column(JSON, default={
        "initial_interval": 500,  # Fixed 500 word intervals
        "multiplier": 1.0,        # No progression, keep it simple
        "max_interval": 500,      # Same as initial (fixed)
        "enabled": True
    })
    
    # Output Template (defines what the agent produces)
    output_template = Column(JSON, default={
        "executive": "Brief executive summary of the meeting",
        "bullets": [
            "Key topic or discussion point",
            "Important decision or outcome", 
            "Notable participant contribution"
        ],
        "actions": [
            {
                "action": "Specific task or follow-up",
                "owner": "Person responsible (if mentioned)",
                "priority": "high|medium|low"
            }
        ],
        "decisions": [
            "Important decision made during meeting"
        ],
        "title": "Suggested meeting title based on content"
    })
    
    # Status
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=True)
    
    # Metadata
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    def to_dict(self):
        """Convert to dictionary for API responses"""
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "personality": self.personality,
            "system_prompt": self.system_prompt,
            "provider_type": self.provider_type,
            "model_name": self.model_name,
            "model_config": self.model_settings,
            "progressive_config": self.progressive_config,
            "output_template": self.output_template,
            "is_active": self.is_active,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }
    
    @classmethod
    def get_default_agent(cls, db_session):
        """Get the default/active agent"""
        return db_session.query(cls).filter(
            cls.is_active == True,
            cls.is_default == True
        ).first()
    
    @classmethod
    def create_default_agent(cls, db_session):
        """Create default agent if none exists"""
        existing = cls.get_default_agent(db_session)
        if existing:
            return existing
        
        default_agent = cls(
            name="Meeting Assistant",
            description="AI assistant for meeting transcription, summarization, and analysis using the configured LLM provider.",
            personality="Professional, concise, and actionable",
            system_prompt="""You are a professional meeting assistant.

Analyze meeting transcripts and provide structured insights:

EXECUTIVE SUMMARY: Provide a 2-3 sentence overview of the meeting's purpose and outcomes.

KEY POINTS: List 3-5 main topics, decisions, or discussion points as bullet points.

ACTION ITEMS: Extract specific tasks, follow-ups, or assignments with owners when mentioned.

DECISIONS: List important decisions, agreements, or conclusions reached.

MEETING TITLE: Suggest a descriptive title based on the meeting content.

Be accurate, actionable, and focus on what matters most for follow-up.""",
            provider_type="ollama",
            model_name="granite3.3:8b",
            model_settings={
                "temperature": 0.7,
                "max_tokens": 1000,
                "top_p": 0.9,
                "num_predict": 500
            },
            progressive_config={
                "initial_interval": 500,  # Fixed 500 word intervals
                "multiplier": 1.0,        # No progression
                "max_interval": 500,      # Same as initial (fixed)
                "enabled": True
            },
            is_active=True,
            is_default=True
        )
        
        db_session.add(default_agent)
        db_session.commit()
        return default_agent