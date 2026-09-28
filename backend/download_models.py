#!/usr/bin/env python3
"""Download ONNX Whisper models for the transcription service"""

import os
import sys
from huggingface_hub import snapshot_download

def download_onnx_whisper_models():
    """Download ONNX Whisper models from Hugging Face"""
    
    model_id = "onnx-community/whisper-base"
    cache_dir = "./whisper_onnx_cache"
    
    print(f"📥 Downloading ONNX Whisper model: {model_id}")
    print(f"📁 Cache directory: {cache_dir}")
    
    try:
        # Download the model
        snapshot_path = snapshot_download(
            repo_id=model_id,
            cache_dir=cache_dir,
            local_files_only=False,
            resume_download=True
        )
        
        print(f"✅ Model downloaded successfully!")
        print(f"📍 Model path: {snapshot_path}")
        
        # List the downloaded files
        print("\n📋 Downloaded files:")
        for root, dirs, files in os.walk(snapshot_path):
            for file in files:
                if file.endswith('.onnx'):
                    file_path = os.path.join(root, file)
                    print(f"  - {file_path}")
        
        return True
        
    except Exception as e:
        print(f"❌ Error downloading model: {e}")
        return False

if __name__ == "__main__":
    success = download_onnx_whisper_models()
    sys.exit(0 if success else 1)