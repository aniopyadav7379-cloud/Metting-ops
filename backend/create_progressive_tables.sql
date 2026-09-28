-- Progressive Meeting Agent System - Simplified Schema
-- Matches frontend requirements exactly

-- Meeting Agents Table (matches frontend requirements)
CREATE TABLE IF NOT EXISTS meeting_agents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    role VARCHAR(50) NOT NULL,
    personality TEXT NOT NULL,
    capabilities JSONB NOT NULL,
    sections JSONB NOT NULL,
    trigger_type VARCHAR(20) NOT NULL DEFAULT 'progressive',
    progressive_config JSONB,
    trigger_value INTEGER,
    model_preference VARCHAR(100),
    is_active BOOLEAN DEFAULT FALSE,
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- AI Providers Table (matches frontend requirements)
CREATE TABLE IF NOT EXISTS ai_providers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    type VARCHAR(50) NOT NULL,
    api_url VARCHAR(500) NOT NULL,
    api_key_encrypted TEXT,
    models JSONB DEFAULT '[]',
    is_active BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_meeting_agents_active ON meeting_agents(is_active);
CREATE INDEX IF NOT EXISTS idx_ai_providers_active ON ai_providers(is_active);

-- Insert default agent (Action Tracker)
INSERT INTO meeting_agents (name, description, role, personality, capabilities, sections, trigger_type, progressive_config, is_active, is_default) VALUES
(
    'Quick Bullets Agent',
    'Fast action-focused summaries with bullet points',
    'action-tracker',
    'Direct, concise, action-oriented. Focuses on what needs to be done.',
    '{"quick_response": true, "bullet_format": true}',
    '{"decisions": "Key decisions made", "actions": "Action items", "next_steps": "Next steps"}',
    'progressive',
    '{"initialWordCount": 50, "intervalMultiplier": 1.2, "maxInterval": 200, "modelSize": "small"}',
    true,
    true
)
ON CONFLICT DO NOTHING;

-- Insert default provider (Local Ollama)
INSERT INTO ai_providers (name, type, api_url, models, is_active) VALUES
(
    'Local Ollama',
    'ollama',
    'http://localhost:11434',
    '[
        {"name": "granite3.3:8b", "size": "large"},
        {"name": "phi4-mini:3.8b", "size": "medium"},
        {"name": "gemma3n:e4b", "size": "small"}
    ]',
    true
)
ON CONFLICT DO NOTHING;