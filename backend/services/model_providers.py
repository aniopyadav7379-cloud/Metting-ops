"""
Model Provider Abstraction Layer
Supports multiple LLM providers with unified interface
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, AsyncGenerator
import aiohttp
import json
from dataclasses import dataclass
import os
import sys
from middleware.request_context import outbound_request_headers


@dataclass
class ModelResponse:
    """Unified response format from any model provider"""
    content: str
    model: str
    provider: str
    usage: Optional[Dict[str, int]] = None
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class ModelParameters:
    """Unified parameters that work across providers"""
    temperature: float = 0.7
    max_tokens: int = 2048
    top_p: float = 0.9
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    stop_sequences: List[str] = None
    stream: bool = False
    
    # Provider-specific parameters stored here
    provider_params: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.stop_sequences is None:
            self.stop_sequences = []
        if self.provider_params is None:
            self.provider_params = {}


class ModelProvider(ABC):
    """Base class for all model providers"""
    
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        parameters: Optional[ModelParameters] = None
    ) -> ModelResponse:
        """Generate a response from the model"""
        pass
    
    @abstractmethod
    async def stream_generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        parameters: Optional[ModelParameters] = None
    ) -> AsyncGenerator[str, None]:
        """Stream responses from the model"""
        pass
    
    @abstractmethod
    async def list_models(self) -> List[str]:
        """List available models from this provider"""
        pass
    
    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the provider is available"""
        pass


class OllamaProvider(ModelProvider):
    """Ollama provider with full parameter control"""
    
    def __init__(self, host: str = "http://localhost:11434", model_name: str = "granite3.3:8b"):
        self.host = host
        self.model_name = model_name
        try:
            import ollama
            self.client = ollama.AsyncClient(host=host)
        except ImportError:
            raise ImportError("ollama package not installed - use OpenAI-compatible provider instead")
    
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        parameters: Optional[ModelParameters] = None
    ) -> ModelResponse:
        """Generate response using Ollama"""
        if parameters is None:
            parameters = ModelParameters()
        
        # Build the messages
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        # Map parameters to Ollama format
        options = {
            "temperature": parameters.temperature,
            "num_predict": parameters.max_tokens,
            "top_p": parameters.top_p,
            "stop": parameters.stop_sequences,
        }
        
        # Add Ollama-specific parameters
        if "num_ctx" in parameters.provider_params:
            options["num_ctx"] = parameters.provider_params["num_ctx"]
        if "num_gpu" in parameters.provider_params:
            options["num_gpu"] = parameters.provider_params["num_gpu"]
        if "num_thread" in parameters.provider_params:
            options["num_thread"] = parameters.provider_params["num_thread"]
        if "repeat_penalty" in parameters.provider_params:
            options["repeat_penalty"] = parameters.provider_params["repeat_penalty"]
        if "seed" in parameters.provider_params:
            options["seed"] = parameters.provider_params["seed"]
        
        try:
            response = await self.client.chat(
                model=self.model_name,
                messages=messages,
                options=options,
                stream=False
            )
            
            return ModelResponse(
                content=response['message']['content'],
                model=self.model_name,
                provider="ollama",
                usage={
                    "prompt_tokens": response.get('prompt_eval_count', 0),
                    "completion_tokens": response.get('eval_count', 0),
                    "total_tokens": response.get('prompt_eval_count', 0) + response.get('eval_count', 0)
                },
                metadata={
                    "eval_duration": response.get('eval_duration'),
                    "load_duration": response.get('load_duration')
                }
            )
        except Exception as e:
            raise Exception(f"Ollama generation failed: {str(e)}")
    
    async def stream_generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        parameters: Optional[ModelParameters] = None
    ) -> AsyncGenerator[str, None]:
        """Stream responses from Ollama"""
        if parameters is None:
            parameters = ModelParameters()
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        options = {
            "temperature": parameters.temperature,
            "num_predict": parameters.max_tokens,
            "top_p": parameters.top_p,
            "stop": parameters.stop_sequences,
        }
        
        # Add provider-specific parameters
        options.update(parameters.provider_params)
        
        try:
            async for chunk in await self.client.chat(
                model=self.model_name,
                messages=messages,
                options=options,
                stream=True
            ):
                if chunk.get('message', {}).get('content'):
                    yield chunk['message']['content']
        except Exception as e:
            raise Exception(f"Ollama streaming failed: {str(e)}")
    
    async def list_models(self) -> List[str]:
        """List available Ollama models"""
        try:
            response = await self.client.list()
            return [model['name'] for model in response.get('models', [])]
        except Exception:
            return []
    
    async def health_check(self) -> bool:
        """Check if Ollama is running"""
        try:
            await self.client.list()
            return True
        except Exception:
            return False


class OpenAIProvider(ModelProvider):
    """OpenAI API provider (also works with OpenAI-compatible APIs)"""
    
    def __init__(
        self,
        api_key: str,
        model_name: str = "gpt-4",
        api_base: str = "https://api.openai.com/v1"
    ):
        self.api_key = api_key
        self.model_name = model_name
        self.api_base = api_base
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
    
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        parameters: Optional[ModelParameters] = None
    ) -> ModelResponse:
        """Generate response using OpenAI API"""
        if parameters is None:
            parameters = ModelParameters()
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": parameters.temperature,
            "max_tokens": parameters.max_tokens,
            "top_p": parameters.top_p,
            "frequency_penalty": parameters.frequency_penalty,
            "presence_penalty": parameters.presence_penalty,
            "stop": parameters.stop_sequences if parameters.stop_sequences else None
        }
        
        # Add OpenAI-specific parameters
        if "response_format" in parameters.provider_params:
            payload["response_format"] = parameters.provider_params["response_format"]
        if "tools" in parameters.provider_params:
            payload["tools"] = parameters.provider_params["tools"]
        
        async with aiohttp.ClientSession(headers=outbound_request_headers()) as session:
            async with session.post(
                f"{self.api_base}/chat/completions",
                headers=self.headers,
                json=payload
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"OpenAI API error: {error_text}")
                
                data = await response.json()
                
                return ModelResponse(
                    content=data['choices'][0]['message']['content'],
                    model=self.model_name,
                    provider="openai",
                    usage=data.get('usage', {}),
                    metadata={
                        "finish_reason": data['choices'][0].get('finish_reason'),
                        "id": data.get('id')
                    }
                )
    
    async def stream_generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        parameters: Optional[ModelParameters] = None
    ) -> AsyncGenerator[str, None]:
        """Stream responses from OpenAI"""
        if parameters is None:
            parameters = ModelParameters()
        
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        
        payload = {
            "model": self.model_name,
            "messages": messages,
            "temperature": parameters.temperature,
            "max_tokens": parameters.max_tokens,
            "top_p": parameters.top_p,
            "stream": True
        }
        
        async with aiohttp.ClientSession(headers=outbound_request_headers()) as session:
            async with session.post(
                f"{self.api_base}/chat/completions",
                headers=self.headers,
                json=payload
            ) as response:
                async for line in response.content:
                    if line:
                        line_text = line.decode('utf-8').strip()
                        if line_text.startswith("data: "):
                            if line_text == "data: [DONE]":
                                break
                            try:
                                data = json.loads(line_text[6:])
                                content = data['choices'][0]['delta'].get('content', '')
                                if content:
                                    yield content
                            except json.JSONDecodeError:
                                continue
    
    async def list_models(self) -> List[str]:
        """List available OpenAI models"""
        async with aiohttp.ClientSession(headers=outbound_request_headers()) as session:
            async with session.get(
                f"{self.api_base}/models",
                headers=self.headers
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return [model['id'] for model in data.get('data', [])]
                return []
    
    async def health_check(self) -> bool:
        """Check if OpenAI API is accessible"""
        try:
            models = await self.list_models()
            return len(models) > 0
        except Exception:
            return False


class AnthropicProvider(ModelProvider):
    """Anthropic Claude API provider"""
    
    def __init__(
        self,
        api_key: str,
        model_name: str = "claude-3-opus-20240229"
    ):
        self.api_key = api_key
        self.model_name = model_name
        self.api_base = "https://api.anthropic.com/v1"
        self.headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json"
        }
    
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        parameters: Optional[ModelParameters] = None
    ) -> ModelResponse:
        """Generate response using Anthropic API"""
        if parameters is None:
            parameters = ModelParameters()
        
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": parameters.max_tokens,
            "temperature": parameters.temperature,
            "top_p": parameters.top_p,
        }
        
        if system_prompt:
            payload["system"] = system_prompt
        
        if parameters.stop_sequences:
            payload["stop_sequences"] = parameters.stop_sequences
        
        async with aiohttp.ClientSession(headers=outbound_request_headers()) as session:
            async with session.post(
                f"{self.api_base}/messages",
                headers=self.headers,
                json=payload
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    raise Exception(f"Anthropic API error: {error_text}")
                
                data = await response.json()
                
                return ModelResponse(
                    content=data['content'][0]['text'],
                    model=self.model_name,
                    provider="anthropic",
                    usage={
                        "prompt_tokens": data['usage']['input_tokens'],
                        "completion_tokens": data['usage']['output_tokens'],
                        "total_tokens": data['usage']['input_tokens'] + data['usage']['output_tokens']
                    },
                    metadata={
                        "id": data.get('id'),
                        "stop_reason": data.get('stop_reason')
                    }
                )
    
    async def stream_generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        parameters: Optional[ModelParameters] = None
    ) -> AsyncGenerator[str, None]:
        """Stream responses from Anthropic"""
        if parameters is None:
            parameters = ModelParameters()
        
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": parameters.max_tokens,
            "temperature": parameters.temperature,
            "stream": True
        }
        
        if system_prompt:
            payload["system"] = system_prompt
        
        async with aiohttp.ClientSession(headers=outbound_request_headers()) as session:
            async with session.post(
                f"{self.api_base}/messages",
                headers=self.headers,
                json=payload
            ) as response:
                async for line in response.content:
                    if line:
                        line_text = line.decode('utf-8').strip()
                        if line_text.startswith("data: "):
                            try:
                                data = json.loads(line_text[6:])
                                if data['type'] == 'content_block_delta':
                                    content = data['delta'].get('text', '')
                                    if content:
                                        yield content
                            except json.JSONDecodeError:
                                continue
    
    async def list_models(self) -> List[str]:
        """List available Anthropic models"""
        # Anthropic doesn't have a list models endpoint
        return [
            "claude-3-opus-20240229",
            "claude-3-sonnet-20240229",
            "claude-3-haiku-20240307",
            "claude-2.1",
            "claude-2.0"
        ]
    
    async def health_check(self) -> bool:
        """Check if Anthropic API is accessible"""
        try:
            # Try a minimal API call
            await self.generate("Hi", parameters=ModelParameters(max_tokens=5))
            return True
        except Exception:
            return False


class ModelProviderFactory:
    """Factory to create appropriate model provider based on configuration"""

    @staticmethod
    def create_provider(
        provider_type: str,
        **kwargs
    ) -> ModelProvider:
        """Create a model provider instance"""

        if provider_type == "ollama":
            return OllamaProvider(
                host=kwargs.get("host", "http://localhost:11434"),
                model_name=kwargs.get("model_name", "granite3.3:8b")
            )

        elif provider_type in ["openai", "llamacpp"]:
            return OpenAIProvider(
                api_key=kwargs.get("api_key", "not-needed"),
                model_name=kwargs.get("model_name", "granite-3.3-2b-instruct"),
                api_base=kwargs.get("api_base", "http://localhost:11437/v1")
            )

        elif provider_type == "anthropic":
            return AnthropicProvider(
                api_key=kwargs.get("api_key"),
                model_name=kwargs.get("model_name", "claude-3-opus-20240229")
            )

        else:
            raise ValueError(f"Unknown provider type: {provider_type}")

    @staticmethod
    def create_from_config(config: Dict[str, Any]) -> ModelProvider:
        """Create provider from a configuration dictionary"""
        provider_type = config.get("provider", "openai_compatible")

        if provider_type == "ollama":
            return OllamaProvider(
                host=config.get("api_endpoint", "http://localhost:11434"),
                model_name=config.get("model_name", "granite3.3:8b")
            )

        elif provider_type in ["openai", "openai_compatible", "llamacpp"]:
            return OpenAIProvider(
                api_key=config.get("api_key", os.getenv("OPENAI_API_KEY", "not-needed")),
                model_name=config.get("model_name", "granite-3.3-2b-instruct"),
                api_base=config.get("api_endpoint", "http://localhost:11437/v1")
            )

        elif provider_type == "anthropic":
            return AnthropicProvider(
                api_key=config.get("api_key", os.getenv("ANTHROPIC_API_KEY", "")),
                model_name=config.get("model_name", "claude-3-opus-20240229")
            )

        else:
            raise ValueError(f"Unknown provider type: {provider_type}")
