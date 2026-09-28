-- Progressive Meeting Agent System Database Schema
-- New tables to support advanced agent configurations and AI providers

-- Meeting Agents Table
CREATE TABLE IF NOT EXISTS meeting_agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    role VARCHAR(50) NOT NULL, -- 'action_tracker', 'meeting_analyst', 'compliance_monitor', etc.
    personality TEXT,
    capabilities JSONB DEFAULT '{}',
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'progressive', -- 'progressive', 'fixed', 'time'
    progressive_config JSONB DEFAULT '{
        "initialWordCount": 50,
        "intervalMultiplier": 1.5,
        "maxInterval": 1000,
        "modelSize": "small"
    }',
    sections JSONB DEFAULT '{}', -- Agent-specific output sections
    prompts JSONB DEFAULT '{}', -- System prompts for different summary types
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- AI Providers Table
CREATE TABLE IF NOT EXISTS ai_providers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    type VARCHAR(50) NOT NULL, -- 'ollama', 'openai', 'anthropic', 'local'
    api_url VARCHAR(500),
    api_key_encrypted TEXT, -- Will store encrypted API keys
    models JSONB DEFAULT '[]', -- Available models from this provider
    capabilities JSONB DEFAULT '{}', -- What this provider can do
    performance_config JSONB DEFAULT '{}', -- Model size mappings, performance settings
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Progressive Summary History Table
CREATE TABLE IF NOT EXISTS progressive_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    agent_id UUID REFERENCES meeting_agents(id),
    provider_id UUID REFERENCES ai_providers(id),
    word_count INTEGER NOT NULL,
    interval_used INTEGER NOT NULL,
    next_interval INTEGER NOT NULL,
    model_size VARCHAR(20) NOT NULL,
    sections JSONB NOT NULL,
    processing_time_ms INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Progressive Session State Table (tracks current intervals per session/agent)
CREATE TABLE IF NOT EXISTS progressive_session_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    agent_id UUID REFERENCES meeting_agents(id),
    current_interval INTEGER NOT NULL,
    last_summary_at INTEGER DEFAULT 0,
    total_summaries INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(session_id, agent_id)
);

-- Create indexes for performance
CREATE INDEX IF NOT EXISTS idx_meeting_agents_active ON meeting_agents(is_active);
CREATE INDEX IF NOT EXISTS idx_meeting_agents_role ON meeting_agents(role);
CREATE INDEX IF NOT EXISTS idx_ai_providers_active ON ai_providers(is_active);
CREATE INDEX IF NOT EXISTS idx_ai_providers_type ON ai_providers(type);
CREATE INDEX IF NOT EXISTS idx_progressive_summaries_session ON progressive_summaries(session_id);
CREATE INDEX IF NOT EXISTS idx_progressive_summaries_agent ON progressive_summaries(agent_id);
CREATE INDEX IF NOT EXISTS idx_progressive_session_state_session ON progressive_session_state(session_id);

-- Insert default agents
INSERT INTO meeting_agents (name, description, role, personality, progressive_config, sections, prompts) VALUES
(
    'Action Tracker',
    'Quick action-focused summaries with bullet points',
    'action_tracker',
    'Direct, concise, action-oriented. Focuses on what needs to be done.',
    '{
        "initialWordCount": 50,
        "intervalMultiplier": 1.2,
        "maxInterval": 200,
        "modelSize": "small"
    }',
    '{
        "actions": "List of action items",
        "decisions": "Key decisions made",
        "next_steps": "Immediate next steps"
    }',
    '{
        "system": "You are an action-focused meeting assistant. Generate concise bullet points for actions, decisions, and next steps. Be direct and specific.",
        "summary": "Extract action items, decisions, and next steps from this meeting transcript:"
    }'
),
(
    'Meeting Analyst',
    'Comprehensive meeting analysis with detailed insights',
    'meeting_analyst',
    'Analytical, thorough, strategic. Provides deep insights and context.',
    '{
        "initialWordCount": 200,
        "intervalMultiplier": 2.0,
        "maxInterval": 1000,
        "modelSize": "large"
    }',
    '{
        "executive": "Executive summary",
        "analysis": "Deep analysis of discussion",
        "insights": "Key insights and implications",
        "recommendations": "Strategic recommendations"
    }',
    '{
        "system": "You are a strategic meeting analyst. Provide comprehensive analysis, insights, and recommendations. Focus on broader implications and strategic thinking.",
        "summary": "Analyze this meeting transcript and provide strategic insights:"
    }'
),
(
    'Compliance Monitor',
    'Monitors for compliance, risks, and regulatory mentions',
    'compliance_monitor',
    'Cautious, detail-oriented, risk-aware. Focuses on compliance and regulatory issues.',
    '{
        "initialWordCount": 100,
        "intervalMultiplier": 1.5,
        "maxInterval": 500,
        "modelSize": "medium"
    }',
    '{
        "risks": "Identified risks",
        "compliance": "Compliance mentions",
        "regulatory": "Regulatory considerations",
        "concerns": "Areas of concern"
    }',
    '{
        "system": "You are a compliance monitoring assistant. Identify risks, regulatory mentions, compliance issues, and areas of concern.",
        "summary": "Review this meeting transcript for compliance, regulatory, and risk factors:"
    }'
);

-- Insert default AI provider (Ollama local)
INSERT INTO ai_providers (name, type, api_url, models, capabilities, performance_config, is_active) VALUES
(
    'Local Ollama',
    'ollama',
    'http://localhost:11434',
    '[
        {"name": "granite3.3:8b", "size": "large", "capabilities": ["chat", "analysis"]},
        {"name": "phi4-mini:3.8b", "size": "medium", "capabilities": ["chat", "summary"]},
        {"name": "gemma3n:e4b", "size": "small", "capabilities": ["chat", "quick_summary"]}
    ]',
    '{
        "streaming": true,
        "context_window": 32768,
        "supports_json": true,
        "local": true
    }',
    '{
        "small": "gemma3n:e4b",
        "medium": "phi4-mini:3.8b", 
        "large": "granite3.3:8b",
        "default_temperature": 0.7,
        "default_top_p": 0.9
    }',
    true
);

-- Set default active agent (Action Tracker)
UPDATE meeting_agents SET is_active = true WHERE role = 'action_tracker';

COMMIT;