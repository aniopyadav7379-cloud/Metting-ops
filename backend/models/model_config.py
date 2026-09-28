"""
Model Configuration System
Decoupled from agent configuration to support multiple providers
Similar to OpenWebUI's parameter system
"""
from sqlalchemy import Column, String, Integer, Float, Boolean, JSON, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from uuid import uuid4

try:
    from backend.database import Base
except ImportError:
    from database import Base


class ModelProvider(Base):
    """Model provider configuration (Ollama, OpenAI, Anthropic, etc.)"""
    __tablename__ = "model_providers"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    name = Column(String(100), nullable=False, unique=True)
    provider_type = Column(String(50), nullable=False)  # 'ollama', 'openai', 'anthropic', 'azure'
    
    # Connection settings
    api_url = Column(String(500))
    api_key = Column(Text)  # Encrypted in production
    api_version = Column(String(50))  # For Azure OpenAI
    
    # Provider-specific settings
    settings = Column(JSON, default=dict)  # Extra provider settings
    headers = Column(JSON, default=dict)   # Custom headers if needed
    
    # Status
    is_active = Column(Boolean, default=True)
    is_verified = Column(Boolean, default=False)
    last_health_check = Column(DateTime)
    
    # Relations
    models = relationship("ModelConfiguration", back_populates="provider")
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class ModelConfiguration(Base):
    """Individual model configuration with parameters"""
    __tablename__ = "model_configurations"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    name = Column(String(100), nullable=False)  # Display name
    model_id = Column(String(200), nullable=False)  # Actual model ID (e.g., 'gpt-4', 'granite3.3:8b')
    
    # Provider relationship
    provider_id = Column(String, ForeignKey("model_providers.id"))
    provider = relationship("ModelProvider", back_populates="models")
    
    # Model capabilities
    capabilities = Column(JSON, default=lambda: {
        "chat": True,
        "completion": False,
        "embedding": False,
        "vision": False,
        "function_calling": False,
        "streaming": True
    })
    
    # Context and token limits
    max_context_length = Column(Integer, default=4096)
    max_output_tokens = Column(Integer, default=2048)
    
    # Default parameters (can be overridden per request)
    default_parameters = Column(JSON, default=lambda: {
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 40,
        "frequency_penalty": 0.0,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.1,
        "seed": None
    })
    
    # Ollama-specific parameters
    ollama_parameters = Column(JSON, default=lambda: {
        "num_gpu": 99,           # GPU layers to offload
        "num_thread": 8,         # CPU threads
        "num_batch": 512,        # Batch size
        "num_ctx": 8192,         # Context window
        "num_predict": 2048,     # Max tokens to predict
        "mirostat": 0,           # Mirostat sampling
        "mirostat_eta": 0.1,
        "mirostat_tau": 5.0,
        "tfs_z": 1.0,            # Tail-free sampling
        "typical_p": 1.0,        # Typical sampling
        "repeat_last_n": 64,     # Repeat penalty context
        "penalize_newline": False,
        "stop": []               # Stop sequences
    })
    
    # Performance settings
    performance_profile = Column(String(50), default="balanced")  # 'speed', 'balanced', 'quality'
    request_timeout = Column(Integer, default=120)  # Seconds
    stream_timeout = Column(Integer, default=300)  # For streaming responses
    
    # Cost tracking (for paid APIs)
    input_cost_per_1k = Column(Float, default=0.0)  # Cost per 1k input tokens
    output_cost_per_1k = Column(Float, default=0.0)  # Cost per 1k output tokens
    
    # Usage tracking
    total_requests = Column(Integer, default=0)
    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)
    total_errors = Column(Integer, default=0)
    
    # Metadata
    description = Column(Text)
    tags = Column(JSON, default=list)  # ['fast', 'meeting', 'transcription']
    is_active = Column(Boolean, default=True)
    is_default = Column(Boolean, default=False)  # Default for its category
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    created_by = Column(Integer, ForeignKey("users.id"))


class ModelParameterSet(Base):
    """Named parameter sets that can be applied to models"""
    __tablename__ = "model_parameter_sets"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    name = Column(String(100), nullable=False)
    description = Column(Text)
    
    # Parameter presets for different use cases
    parameters = Column(JSON, nullable=False)
    
    # Categorization
    use_case = Column(String(50))  # 'transcription', 'summarization', 'analysis'
    provider_type = Column(String(50))  # Which providers this works with
    
    # Examples of parameter sets:
    # - "Fast Transcription": Low temperature, high speed
    # - "Deep Analysis": Higher temperature, more tokens
    # - "Creative Writing": High temperature, top_p
    
    is_system = Column(Boolean, default=False)  # System-provided vs user-created
    created_by = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class ModelExecutionLog(Base):
    """Track model execution for monitoring and optimization"""
    __tablename__ = "model_execution_logs"
    
    id = Column(String, primary_key=True, default=lambda: str(uuid4()))
    
    # Model and request info
    model_id = Column(String, ForeignKey("model_configurations.id"))
    agent_id = Column(String, ForeignKey("agent_configurations.id"), nullable=True)
    session_id = Column(Integer, ForeignKey("recording_sessions.id"), nullable=True)
    
    # Execution details
    request_type = Column(String(50))  # 'chat', 'completion', 'embedding'
    parameters_used = Column(JSON)  # Actual parameters sent
    
    # Performance metrics
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime)
    response_time_ms = Column(Integer)
    
    # Token usage
    input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    
    # Status and errors
    status = Column(String(50))  # 'success', 'error', 'timeout'
    error_message = Column(Text)
    
    # Response quality metrics (optional)
    quality_score = Column(Float)  # If we implement quality scoring
    user_feedback = Column(String(50))  # 'good', 'bad', 'neutral'
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# Default model configurations
DEFAULT_MODELS = {
    "ollama": {
        "phi4-mini": {
            "name": "Phi4 Mini Fast",
            "model_id": "phi4-mini:3.8b",
            "description": "Fast 3.8B model optimized for quick responses",
            "capabilities": {"chat": True, "streaming": True},
            "max_context_length": 4096,
            "default_parameters": {
                "temperature": 0.5,
                "top_p": 0.8,
                "top_k": 20
            },
            "ollama_parameters": {
                "num_gpu": 99,
                "num_ctx": 4096,
                "num_predict": 1024
            },
            "performance_profile": "speed",
            "tags": ["fast", "lightweight", "early-phase"]
        },
        "granite": {
            "name": "Granite 3.3 Deep",
            "model_id": "granite3.3:8b",
            "description": "8B model for comprehensive analysis",
            "capabilities": {"chat": True, "streaming": True},
            "max_context_length": 8192,
            "default_parameters": {
                "temperature": 0.7,
                "top_p": 0.9,
                "top_k": 40
            },
            "ollama_parameters": {
                "num_gpu": 99,
                "num_ctx": 8192,
                "num_predict": 2048
            },
            "performance_profile": "quality",
            "tags": ["comprehensive", "analysis", "late-phase"]
        }
    },
    "openai": {
        "gpt-4": {
            "name": "GPT-4",
            "model_id": "gpt-4",
            "description": "OpenAI's most capable model",
            "capabilities": {"chat": True, "vision": True, "function_calling": True},
            "max_context_length": 8192,
            "default_parameters": {
                "temperature": 0.7,
                "top_p": 0.9,
                "frequency_penalty": 0.0,
                "presence_penalty": 0.0
            },
            "performance_profile": "quality",
            "input_cost_per_1k": 0.03,
            "output_cost_per_1k": 0.06
        },
        "gpt-3.5-turbo": {
            "name": "GPT-3.5 Turbo",
            "model_id": "gpt-3.5-turbo",
            "description": "Fast and cost-effective",
            "capabilities": {"chat": True, "function_calling": True},
            "max_context_length": 4096,
            "default_parameters": {
                "temperature": 0.7,
                "top_p": 0.9
            },
            "performance_profile": "balanced",
            "input_cost_per_1k": 0.0015,
            "output_cost_per_1k": 0.002
        }
    },
    "anthropic": {
        "claude-3-opus": {
            "name": "Claude 3 Opus",
            "model_id": "claude-3-opus-20240229",
            "description": "Most capable Claude model",
            "capabilities": {"chat": True, "vision": True},
            "max_context_length": 200000,
            "default_parameters": {
                "temperature": 0.7,
                "top_p": 0.9,
                "top_k": 40
            },
            "performance_profile": "quality",
            "input_cost_per_1k": 0.015,
            "output_cost_per_1k": 0.075
        },
        "claude-3-sonnet": {
            "name": "Claude 3 Sonnet",
            "model_id": "claude-3-sonnet-20240229",
            "description": "Balanced performance and cost",
            "capabilities": {"chat": True, "vision": True},
            "max_context_length": 200000,
            "default_parameters": {
                "temperature": 0.7,
                "top_p": 0.9
            },
            "performance_profile": "balanced",
            "input_cost_per_1k": 0.003,
            "output_cost_per_1k": 0.015
        }
    }
}


# Parameter presets
DEFAULT_PARAMETER_SETS = [
    {
        "name": "Fast Transcription",
        "description": "Optimized for quick transcript formatting",
        "use_case": "transcription",
        "parameters": {
            "temperature": 0.3,
            "top_p": 0.7,
            "top_k": 10,
            "max_tokens": 1024,
            "ollama": {
                "num_predict": 1024,
                "repeat_penalty": 1.0
            }
        }
    },
    {
        "name": "Deep Analysis",
        "description": "Comprehensive meeting analysis",
        "use_case": "analysis",
        "parameters": {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "max_tokens": 4096,
            "ollama": {
                "num_predict": 4096,
                "repeat_penalty": 1.1
            }
        }
    },
    {
        "name": "Balanced",
        "description": "Good balance of speed and quality",
        "use_case": "general",
        "parameters": {
            "temperature": 0.5,
            "top_p": 0.85,
            "top_k": 30,
            "max_tokens": 2048
        }
    },
    {
        "name": "Creative",
        "description": "More creative and varied outputs",
        "use_case": "creative",
        "parameters": {
            "temperature": 0.9,
            "top_p": 0.95,
            "top_k": 50,
            "frequency_penalty": 0.5,
            "presence_penalty": 0.5
        }
    }
]