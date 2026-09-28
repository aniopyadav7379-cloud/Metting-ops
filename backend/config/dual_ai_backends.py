"""
AI Backend Configuration - Single Model
All tasks use Granite 3.3 2B on llama.cpp Vulkan at port 11437
(Kept for backward compatibility with unified_agent_service imports)
"""
import os
import logging
import requests

logger = logging.getLogger(__name__)

# Single backend configuration - Granite 3.3 2B for all tasks
AI_BACKENDS = {
    "granite_2b": {
        "url": os.getenv("LLAMACPP_URL", "http://localhost:11437"),
        "endpoint": "/v1/chat/completions",
        "type": "llamacpp",
        "name": "Granite 3.3 2B (llama.cpp Vulkan)",
        "model": "granite-3.3-2b-instruct",
        "timeout": 30,
        "health_check": "/health",
        "use_for": ["progressive", "live", "realtime", "final", "comprehensive", "post_meeting"]
    }
}

def check_backend_health(backend_config: dict) -> bool:
    """Check if backend is available"""
    try:
        url = f"{backend_config['url']}{backend_config['health_check']}"
        response = requests.get(url, timeout=2)
        return response.status_code == 200
    except Exception as e:
        logger.debug(f"Backend health check failed: {e}")
        return False

def get_backend_for_task(task_type: str = "progressive"):
    """Get backend for any task - always returns Granite 3.3 2B"""
    backend_name = "granite_2b"
    backend = AI_BACKENDS[backend_name]

    if check_backend_health(backend):
        logger.info(f"Using {backend['name']} for {task_type} task")
        return backend_name, backend

    logger.warning(f"Granite 3.3 2B not responding for {task_type} task, returning config anyway")
    return backend_name, backend

def get_live_summary_backend():
    """Get backend for live/progressive summaries"""
    return get_backend_for_task("progressive")

def get_final_summary_backend():
    """Get backend for final comprehensive summaries"""
    return get_backend_for_task("final")
