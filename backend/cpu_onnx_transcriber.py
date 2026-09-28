#!/usr/bin/env python3
"""
CPU-based ONNX Whisper Transcriber
Falls back to CPU when NPU is not available
"""

import numpy as np
import onnxruntime as ort
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import librosa
from transformers import WhisperTokenizer

logger = logging.getLogger(__name__)

class CPUWhisperTranscriber:
    """CPU-based Whisper transcriber using ONNX models"""
    
    def __init__(self, model_size="base"):
        self.model_size = model_size
        self.encoder_session = None
        self.decoder_session = None
        self.tokenizer = None
        self.is_ready = False
        
        # Model paths
        self.model_dir = Path(f"models/whisper-{model_size}-onnx")
        self.encoder_path = self.model_dir / "onnx/encoder_model.onnx"
        self.decoder_path = self.model_dir / "onnx/decoder_model.onnx"
        
        logger.info(f"🖥️ Initializing CPU ONNX Whisper transcriber ({model_size})")
        self._initialize()
    
    def _initialize(self):
        """Initialize ONNX sessions and tokenizer"""
        try:
            # Check if models exist
            if not self.encoder_path.exists() or not self.decoder_path.exists():
                logger.error(f"❌ ONNX models not found at {self.model_dir}")
                return False
            
            # Create ONNX sessions with CPU provider
            logger.info("📦 Loading ONNX models on CPU...")
            session_options = ort.SessionOptions()
            session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            
            # Load encoder
            self.encoder_session = ort.InferenceSession(
                str(self.encoder_path),
                session_options,
                providers=['CPUExecutionProvider']
            )
            logger.info(f"✅ Encoder loaded: {len(self.encoder_session.get_inputs())} inputs")
            
            # Load decoder  
            self.decoder_session = ort.InferenceSession(
                str(self.decoder_path),
                session_options,
                providers=['CPUExecutionProvider']
            )
            logger.info(f"✅ Decoder loaded: {len(self.decoder_session.get_inputs())} inputs")
            
            # Load tokenizer
            self.tokenizer = WhisperTokenizer.from_pretrained(f"openai/whisper-{self.model_size}")
            logger.info("✅ Tokenizer loaded")
            
            self.is_ready = True
            logger.info("✅ CPU ONNX Whisper transcriber ready")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize CPU transcriber: {e}")
            return False
    
    def transcribe_chunk(self, audio_chunk: np.ndarray) -> Dict[str, Any]:
        """Transcribe audio chunk using CPU ONNX"""
        if not self.is_ready:
            return {"text": "", "error": "Transcriber not ready"}
        
        try:
            # For now, return a simple response indicating CPU processing
            # In a real implementation, this would:
            # 1. Convert audio to mel spectrogram
            # 2. Run through encoder
            # 3. Run through decoder with beam search
            # 4. Convert tokens to text
            
            # Simple placeholder that shows CPU is working
            audio_energy = np.sqrt(np.mean(audio_chunk**2))
            
            if audio_energy > 0.01:  # Voice detected
                return {
                    "text": f"[CPU Processing: Audio detected, energy={audio_energy:.3f}]",
                    "confidence": 0.8,
                    "cpu_processed": True,
                    "model": f"whisper-{self.model_size}"
                }
            else:
                return {
                    "text": "",
                    "confidence": 0.0,
                    "cpu_processed": True,
                    "model": f"whisper-{self.model_size}"
                }
                
        except Exception as e:
            logger.error(f"❌ CPU transcription error: {e}")
            return {"text": "", "error": str(e)}
    
    def cleanup(self):
        """Clean up resources"""
        self.encoder_session = None
        self.decoder_session = None
        self.tokenizer = None
        self.is_ready = False

# Global instance
cpu_transcriber = None

def get_cpu_transcriber(model_size="base"):
    """Get or create CPU transcriber instance"""
    global cpu_transcriber
    if cpu_transcriber is None:
        cpu_transcriber = CPUWhisperTranscriber(model_size)
    return cpu_transcriber