"""
AI Backend Configuration
Reads active model from model_registry, exposes llama.cpp Vulkan backend config.
"""
import os
import logging

logger = logging.getLogger(__name__)

# Default to GPU-accelerated backend
AI_BACKEND = os.getenv("AI_BACKEND", "llamacpp")


def _build_llamacpp_config() -> dict:
    """Build llama.cpp backend config using the active model from registry."""
    try:
        from config.model_registry import get_active_model
        active = get_active_model()
        model_id = active["model_id"]
        display_name = active.get("display_name", model_id)
    except Exception:
        model_id = "gpt-oss-20b"
        display_name = "GPT-OSS 20B (MXFP4)"

    return {
        "url": os.getenv("LLAMACPP_URL", "http://localhost:11437"),
        "endpoint": "/v1/chat/completions",
        "type": "llamacpp",
        "name": f"{display_name} (llama.cpp Vulkan)",
        "model": model_id,
        "timeout": 60,
        "health_check": "/health",
    }


# Backend configurations — built lazily so model_registry is resolved at call time
AI_BACKENDS = {
    "llamacpp": _build_llamacpp_config(),
}


def get_ai_backend(backend_name: str = None):
    """Get AI backend configuration"""
    backend_name = backend_name or AI_BACKEND

    if backend_name not in AI_BACKENDS:
        logger.warning(f"Unknown backend {backend_name}, falling back to llamacpp")
        backend_name = "llamacpp"

    # Rebuild to pick up any model changes
    backend = _build_llamacpp_config() if backend_name == "llamacpp" else AI_BACKENDS[backend_name].copy()
    logger.info(f"Using AI backend: {backend['name']} at {backend['url']}")
    return backend


def check_backend_health(backend_config: dict) -> bool:
    """Check if backend is available"""
    import requests
    try:
        url = f"{backend_config['url']}{backend_config['health_check']}"
        response = requests.get(url, timeout=2)
        return response.status_code == 200
    except Exception as e:
        logger.debug(f"Backend health check failed: {e}")
        return False


def get_available_backend():
    """Get the active LLM backend"""
    backend_name = "llamacpp"
    backend = _build_llamacpp_config()

    if check_backend_health(backend):
        logger.info(f"{backend['name']} is available")
        return backend_name, backend

    # If GPU backend not available, warn but return config anyway
    logger.warning("GPU backend not responding - ensure llama.cpp is running on port 11437")
    logger.warning("   Start with: docker compose -f docker-compose-full-stack.yml up -d")
    return backend_name, backend
