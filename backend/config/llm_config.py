"""
LLM Configuration System for Meeting-Ops
Supports multiple endpoints with selection and fallback
"""

import os
import json
from typing import Dict, List, Optional, Any
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field
import logging

logger = logging.getLogger(__name__)

class LLMProvider(str, Enum):
    """Available LLM providers"""
    LOCAL_OLLAMA = "local_ollama"
    REMOTE_OLLAMA = "remote_ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    CUSTOM = "custom"

class LLMEndpoint(BaseModel):
    """Configuration for a single LLM endpoint"""
    name: str
    provider: LLMProvider
    url: str
    api_key: Optional[str] = None
    model: str
    context_window: int = 8192
    max_tokens: int = 4096
    temperature: float = 0.7
    enabled: bool = True
    priority: int = 1  # Lower number = higher priority
    description: Optional[str] = None
    
    model_config = ConfigDict(use_enum_values=True)

class LLMConfig(BaseModel):
    """Main LLM configuration"""
    endpoints: List[LLMEndpoint]
    active_endpoint: Optional[str] = None  # Name of active endpoint
    fallback_enabled: bool = True
    timeout_seconds: int = 30
    retry_attempts: int = 3
    
    def get_active_endpoint(self) -> Optional[LLMEndpoint]:
        """Get the currently active endpoint"""
        if self.active_endpoint:
            for endpoint in self.endpoints:
                if endpoint.name == self.active_endpoint and endpoint.enabled:
                    return endpoint
        
        # Fall back to highest priority enabled endpoint
        enabled = [e for e in self.endpoints if e.enabled]
        if enabled:
            return sorted(enabled, key=lambda x: x.priority)[0]
        return None
    
    def get_fallback_endpoints(self) -> List[LLMEndpoint]:
        """Get fallback endpoints in priority order"""
        if not self.fallback_enabled:
            return []
        
        active = self.get_active_endpoint()
        fallbacks = [e for e in self.endpoints if e.enabled and e != active]
        return sorted(fallbacks, key=lambda x: x.priority)

class LLMConfigManager:
    """Manages LLM configurations with persistence"""
    
    CONFIG_FILE = "llm_config.json"
    DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(__file__), CONFIG_FILE)
    
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or self.DEFAULT_CONFIG_PATH
        self.config = self.load_config()
    
    def load_config(self) -> LLMConfig:
        """Load configuration from file or create default"""
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, 'r') as f:
                    data = json.load(f)
                    return LLMConfig(**data)
            except Exception as e:
                logger.error(f"Failed to load config: {e}")
        
        # Return default configuration
        return self.get_default_config()
    
    def save_config(self) -> None:
        """Save current configuration to file"""
        try:
            os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
            with open(self.config_path, 'w') as f:
                json.dump(self.config.dict(), f, indent=2)
            logger.info(f"Configuration saved to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
    
    def get_default_config(self) -> LLMConfig:
        """Get default configuration with standard endpoints"""
        return LLMConfig(
            endpoints=[
                # Local Ollama with Gemma3n E4B
                LLMEndpoint(
                    name="local_gemma3n",
                    provider=LLMProvider.LOCAL_OLLAMA,
                    url="http://localhost:11434",
                    model="meeting-ops-gemma",
                    context_window=8192,
                    max_tokens=4096,
                    temperature=0.7,
                    enabled=True,
                    priority=1,
                    description="Local Gemma3n E4B optimized for AMD 8945HS"
                ),
                
                # Remote Ollama for testing (Gemma2 9B)
                LLMEndpoint(
                    name="remote_gemma2_9b",
                    provider=LLMProvider.REMOTE_OLLAMA,
                    url="http://192.168.1.135:11434",
                    model="gemma2:9b",
                    context_window=8192,
                    max_tokens=4096,
                    temperature=0.7,
                    enabled=True,
                    priority=2,
                    description="Remote Gemma2 9B for testing"
                ),
                
                # OpenAI GPT-4 (disabled by default)
                LLMEndpoint(
                    name="openai_gpt4",
                    provider=LLMProvider.OPENAI,
                    url="https://api.openai.com/v1",
                    api_key=os.getenv("OPENAI_API_KEY", ""),
                    model="gpt-4-turbo-preview",
                    context_window=128000,
                    max_tokens=4096,
                    temperature=0.7,
                    enabled=False,
                    priority=3,
                    description="OpenAI GPT-4 Turbo"
                ),
                
                # Anthropic Claude (disabled by default)
                LLMEndpoint(
                    name="anthropic_claude",
                    provider=LLMProvider.ANTHROPIC,
                    url="https://api.anthropic.com/v1",
                    api_key=os.getenv("ANTHROPIC_API_KEY", ""),
                    model="claude-3-opus-20240229",
                    context_window=200000,
                    max_tokens=4096,
                    temperature=0.7,
                    enabled=False,
                    priority=4,
                    description="Anthropic Claude 3 Opus"
                )
            ],
            active_endpoint="remote_gemma2_9b",  # Use remote for testing initially
            fallback_enabled=True,
            timeout_seconds=30,
            retry_attempts=3
        )
    
    def add_endpoint(self, endpoint: LLMEndpoint) -> None:
        """Add a new endpoint"""
        # Check for duplicate names
        for existing in self.config.endpoints:
            if existing.name == endpoint.name:
                raise ValueError(f"Endpoint with name '{endpoint.name}' already exists")
        
        self.config.endpoints.append(endpoint)
        self.save_config()
    
    def update_endpoint(self, name: str, updates: Dict[str, Any]) -> None:
        """Update an existing endpoint"""
        for i, endpoint in enumerate(self.config.endpoints):
            if endpoint.name == name:
                # Update fields
                updated_data = endpoint.dict()
                updated_data.update(updates)
                self.config.endpoints[i] = LLMEndpoint(**updated_data)
                self.save_config()
                return
        
        raise ValueError(f"Endpoint '{name}' not found")
    
    def delete_endpoint(self, name: str) -> None:
        """Delete an endpoint"""
        self.config.endpoints = [e for e in self.config.endpoints if e.name != name]
        
        # Update active endpoint if deleted
        if self.config.active_endpoint == name:
            self.config.active_endpoint = None
        
        self.save_config()
    
    def set_active_endpoint(self, name: str) -> None:
        """Set the active endpoint"""
        # Verify endpoint exists
        endpoint_names = [e.name for e in self.config.endpoints]
        if name not in endpoint_names:
            raise ValueError(f"Endpoint '{name}' not found")
        
        self.config.active_endpoint = name
        self.save_config()
    
    def test_endpoint(self, name: str) -> Dict[str, Any]:
        """Test an endpoint connection"""
        endpoint = None
        for e in self.config.endpoints:
            if e.name == name:
                endpoint = e
                break
        
        if not endpoint:
            return {"success": False, "error": "Endpoint not found"}
        
        try:
            if endpoint.provider in [LLMProvider.LOCAL_OLLAMA, LLMProvider.REMOTE_OLLAMA]:
                import requests
                response = requests.get(f"{endpoint.url}/api/tags", timeout=5)
                if response.status_code == 200:
                    models = response.json().get('models', [])
                    model_names = [m.get('name', '') for m in models]
                    has_model = any(endpoint.model in name for name in model_names)
                    
                    return {
                        "success": True,
                        "status": "connected",
                        "models": model_names,
                        "has_configured_model": has_model
                    }
                else:
                    return {
                        "success": False,
                        "error": f"HTTP {response.status_code}"
                    }
            
            # Add tests for other providers as needed
            return {
                "success": False,
                "error": "Provider test not implemented"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

# Global instance
llm_config_manager = LLMConfigManager()