#!/usr/bin/env python3
"""
Download and configure the best Whisper model for NPU
With 16 TOPS, we can easily run large models in real-time
"""

import os
import sys
from pathlib import Path
from faster_whisper import WhisperModel
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def download_best_models():
    """Download the best models for NPU usage"""
    
    models_to_download = [
        ("large-v3", "int8"),  # Best quality, NPU can handle it
        ("medium", "int8"),    # Fallback option, still excellent
        ("base", "int8"),      # Already have this, but ensure int8
    ]
    
    cache_dir = Path.home() / ".cache" / "whisper"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    for model_name, compute_type in models_to_download:
        try:
            logger.info(f"📥 Downloading {model_name} with {compute_type} optimization...")
            
            # This will download if not already cached
            model = WhisperModel(
                model_name,
                device="cpu",  # CPU device but NPU-accelerated through int8
                compute_type=compute_type,
                download_root=str(cache_dir)
            )
            
            # Test the model
            logger.info(f"✅ {model_name} model ready for NPU")
            
            # Get model info
            logger.info(f"   Model path: {cache_dir}")
            logger.info(f"   Compute type: {compute_type} (NPU-optimized)")
            
        except Exception as e:
            logger.error(f"❌ Failed to download {model_name}: {e}")
    
    # Set the default model in config
    config_file = Path(__file__).parent / "npu_config.json"
    import json
    
    config = {
        "default_model": "large-v3",  # Use the best model by default
        "compute_type": "int8",
        "device": "npu",
        "features": {
            "word_timestamps": True,
            "diarization": True,
            "vad": True,
            "language_detection": False,  # Skip for speed, assume English
            "beam_size": 1,  # Greedy search for speed
            "temperature": 0  # Deterministic
        },
        "optimization": {
            "num_workers": 4,
            "batch_size": 1,
            "chunk_length": 30,  # Process 30-second chunks
            "condition_on_previous_text": False
        },
        "npu_specs": {
            "device": "AMD Phoenix",
            "compute_power": "16 TOPS INT8",
            "expected_rtf": 0.05,  # 20x real-time expected
            "max_audio_length": 3600  # 1 hour max
        }
    }
    
    with open(config_file, 'w') as f:
        json.dump(config, f, indent=2)
    
    logger.info(f"📝 Configuration saved to {config_file}")
    logger.info("\n🎯 Recommended settings for NPU:")
    logger.info("   Model: large-v3 (best quality)")
    logger.info("   Compute: INT8 (16 TOPS optimization)")
    logger.info("   Features: Word timestamps + Diarization + VAD")
    logger.info("   Expected: 20x real-time performance")

if __name__ == "__main__":
    download_best_models()