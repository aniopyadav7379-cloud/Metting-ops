#!/usr/bin/env python3
"""
Create agent management tables and initialize core agents
"""
import sys
import logging
from datetime import datetime
import uuid

# Add backend to path
sys.path.insert(0, '/srv/meeting-ops/backend')

from database.database import engine, SessionLocal
from models.agent_system import MeetingAgent, AgentTemplate, AgentSession, AgentType, AgentPermission
from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def create_tables():
    """Create all agent-related tables"""
    try:
        # Import Base from the agent_system module
        from models.agent_system import Base
        
        # Create tables
        Base.metadata.create_all(bind=engine)
        logger.info("✅ Agent tables created successfully")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to create tables: {e}")
        return False

def initialize_core_agents():
    """Create the two core pipeline agents"""
    db = SessionLocal()
    try:
        # Check if core agents already exist
        live_agent = db.query(MeetingAgent).filter_by(slug="core-live-summary").first()
        final_agent = db.query(MeetingAgent).filter_by(slug="core-final-analysis").first()
        
        if not live_agent:
            # Create Live Summary Agent (Qwen3-0.6B)
            live_agent = MeetingAgent(
                id=uuid.uuid4(),
                name="Live Summary Agent",
                slug="core-live-summary",
                description="Ultra-fast real-time meeting summarization",
                icon="⚡",
                agent_type=AgentType.CORE_LIVE,
                purpose="Provides real-time progressive summaries during meeting recording",
                is_core=True,
                is_active=True,
                is_default_live=True,
                is_default_final=False,
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
                output_template=None,
                progressive_config={
                    "enabled": True,
                    "initial_interval": 500,
                    "multiplier": 1.0,
                    "max_interval": 500
                },
                chat_config={
                    "max_history": 0,
                    "include_context": False,
                    "rag_enabled": False,
                    "vector_search": False
                },
                permission_level=AgentPermission.AUTHENTICATED,
                allowed_roles=["user", "admin"],
                created_by=uuid.UUID("00000000-0000-0000-0000-000000000000"),  # System user
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                version="1.0.0",
                usage_count=0,
                performance_metrics={
                    "avg_response_time": 0,
                    "avg_tokens_per_sec": 123.63,
                    "success_rate": 100,
                    "model_size": "0.6B",
                    "context_window": "32K"
                }
            )
            db.add(live_agent)
            logger.info("✅ Created Live Summary Agent (Qwen3-0.6B, 123.63 t/s)")
        else:
            logger.info("ℹ️ Live Summary Agent already exists")
        
        if not final_agent:
            # Create Final Analysis Agent (Qwen3-30B)
            final_agent = MeetingAgent(
                id=uuid.uuid4(),
                name="Final Analysis Agent",
                slug="core-final-analysis",
                description="Comprehensive meeting analysis with structured output",
                icon="🎯",
                agent_type=AgentType.CORE_FINAL,
                purpose="Provides detailed structured analysis after meeting completion",
                is_core=True,
                is_active=True,
                is_default_live=False,
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
                progressive_config={
                    "enabled": False,
                    "initial_interval": 0,
                    "multiplier": 1.0,
                    "max_interval": 0
                },
                chat_config={
                    "max_history": 0,
                    "include_context": False,
                    "rag_enabled": False,
                    "vector_search": False
                },
                permission_level=AgentPermission.AUTHENTICATED,
                allowed_roles=["user", "admin"],
                created_by=uuid.UUID("00000000-0000-0000-0000-000000000000"),  # System user
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                version="1.0.0",
                usage_count=0,
                performance_metrics={
                    "avg_response_time": 0,
                    "avg_tokens_per_sec": 26.54,
                    "success_rate": 100,
                    "model_size": "30B",
                    "context_window": "32K"
                }
            )
            db.add(final_agent)
            logger.info("✅ Created Final Analysis Agent (Qwen3-30B, 26.54 t/s)")
        else:
            logger.info("ℹ️ Final Analysis Agent already exists")
        
        # Commit changes
        db.commit()
        logger.info("✅ Core agents initialized successfully")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to initialize core agents: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def create_sample_templates():
    """Create some sample agent templates"""
    db = SessionLocal()
    try:
        # Check if templates exist
        existing = db.query(AgentTemplate).first()
        if existing:
            logger.info("ℹ️ Templates already exist")
            return True
        
        templates = [
            AgentTemplate(
                id=uuid.uuid4(),
                name="Standup Meeting Template",
                category="meeting",
                description="Daily standup meeting notes",
                icon="🏃",
                template_data={
                    "name": "Standup Agent",
                    "description": "Captures daily standup updates",
                    "agent_type": "task_template",
                    "system_prompt": "Extract yesterday's work, today's plans, and blockers from standup meeting.",
                    "model_name": "phi-4-mini",
                    "output_format": "json",
                    "output_template": {
                        "yesterday": ["string"],
                        "today": ["string"],
                        "blockers": ["string"]
                    }
                },
                is_official=True,
                downloads=0,
                created_by=uuid.UUID("00000000-0000-0000-0000-000000000000")
            ),
            AgentTemplate(
                id=uuid.uuid4(),
                name="Legal Meeting Analysis",
                category="analysis",
                description="Legal compliance and risk analysis",
                icon="⚖️",
                template_data={
                    "name": "Legal Analyst",
                    "description": "Analyzes meetings for legal implications",
                    "agent_type": "analysis",
                    "system_prompt": "Identify legal risks, compliance issues, and actionable legal items.",
                    "model_name": "granite-3.3-8b",
                    "output_format": "json"
                },
                is_official=True,
                downloads=0,
                created_by=uuid.UUID("00000000-0000-0000-0000-000000000000")
            )
        ]
        
        for template in templates:
            db.add(template)
        
        db.commit()
        logger.info(f"✅ Created {len(templates)} agent templates")
        return True
        
    except Exception as e:
        logger.error(f"❌ Failed to create templates: {e}")
        db.rollback()
        return False
    finally:
        db.close()

def main():
    """Main setup function"""
    logger.info("🚀 Setting up Agent Management System")
    
    # Create tables
    if not create_tables():
        logger.error("Failed to create tables")
        return False
    
    # Initialize core agents
    if not initialize_core_agents():
        logger.error("Failed to initialize core agents")
        return False
    
    # Create sample templates
    if not create_sample_templates():
        logger.warning("Failed to create templates (non-critical)")
    
    logger.info("✅ Agent Management System setup complete!")
    logger.info("   - Core agents created")
    logger.info("   - Live: Qwen3-0.6B at 123.63 tokens/sec")
    logger.info("   - Final: Qwen3-30B at 26.54 tokens/sec")
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)