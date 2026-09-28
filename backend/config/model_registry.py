"""
Model Registry for Meeting-Ops
Centralized model configuration that all services read from.
"""
import logging

logger = logging.getLogger(__name__)

MODEL_REGISTRY = {
    "gpt-oss-20b": {
        "gguf_file": "gpt-oss-20b-mxfp4.gguf",
        "display_name": "GPT-OSS 20B (MXFP4)",
        "max_tokens_multiplier": 3.0,   # Harmony reasoning overhead (36-89% hidden tokens)
        "extra_flags": ["--jinja", "--flash-attn", "on", "--batch-size", "2048", "--ubatch-size", "2048"],
        "ctx_size": 32768,
        "speed_estimate": "~28 tok/s",
        "vram_estimate": "~12GB",
        "best_for": "High-quality summaries, chat, insights, analysis",
    },
    "granite-3.3-2b-instruct": {
        "gguf_file": "granite-3.3-2b-instruct-Q4_K_M.gguf",
        "display_name": "Granite 3.3 2B Instruct (Q4_K_M)",
        "max_tokens_multiplier": 1.0,
        "extra_flags": [],
        "ctx_size": 32768,
        "speed_estimate": "~43 tok/s",
        "vram_estimate": "~1.5GB",
        "best_for": "Fast summaries, lightweight tasks",
    },
}

DEFAULT_MODEL = "gpt-oss-20b"


def get_active_model() -> dict:
    """Get the active model configuration from settings, falling back to DEFAULT_MODEL.

    Returns a dict with all registry fields plus a 'model_id' key.
    """
    model_id = DEFAULT_MODEL
    try:
        from services.settings_manager import settings_manager
        saved = settings_manager.get_setting("ai.llm_model")
        if saved and saved in MODEL_REGISTRY:
            model_id = saved
    except Exception as e:
        logger.debug(f"Could not read ai.llm_model from settings: {e}")

    entry = MODEL_REGISTRY.get(model_id, MODEL_REGISTRY[DEFAULT_MODEL])
    return {"model_id": model_id, **entry}
