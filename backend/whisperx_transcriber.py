#!/usr/bin/env python3
"""
WhisperX-based Transcriber - Uses the real WhisperX library
"""

import numpy as np
import logging
import whisperx
import torch
from typing import Dict, Optional
import gc

logger = logging.getLogger(__name__)

class WhisperXTranscriber:
    """Real WhisperX transcriber"""
    
    def __init__(self, model_size="base", device="cpu", compute_type="int8"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.model = None
        self.is_ready = False
        
        logger.info(f"🎤 Initializing WhisperX transcriber ({model_size}, {device}, {compute_type})")
        self._initialize()
    
    def _initialize(self):
        """Initialize WhisperX model"""
        try:
            # Load model
            logger.info("Loading WhisperX model...")
            self.model = whisperx.load_model(
                self.model_size, 
                self.device,
                compute_type=self.compute_type
            )
            self.is_ready = True
            logger.info("✅ WhisperX model loaded successfully")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize WhisperX: {e}")
            return False
    
    def transcribe_chunk(self, audio_chunk: np.ndarray) -> Dict:
        """Transcribe audio chunk using WhisperX"""
        
        if not self.is_ready or self.model is None:
            return {"text": "", "error": "WhisperX not ready"}
        
        try:
            # WhisperX expects float32 audio at 16kHz
            if audio_chunk.dtype != np.float32:
                audio_chunk = audio_chunk.astype(np.float32)
            
            # Ensure audio is in correct range
            if np.abs(audio_chunk).max() > 1.0:
                audio_chunk = audio_chunk / 32768.0
            
            # Run transcription
            result = self.model.transcribe(audio_chunk, batch_size=1)
            
            # Extract text
            text = result.get("segments", [{}])[0].get("text", "").strip() if result.get("segments") else ""
            
            return {
                "text": text,
                "confidence": 0.9,
                "language": result.get("language", "en"),
                "segments": result.get("segments", []),
                "engine": "whisperx"
            }
            
        except Exception as e:
            logger.error(f"❌ WhisperX transcription error: {e}")
            return {"text": "", "error": str(e)}
    
    def cleanup(self):
        """Clean up resources"""
        if self.model:
            del self.model
            gc.collect()
            torch.cuda.empty_cache() if torch.cuda.is_available() else None
        self.is_ready = False

# Global instance
_whisperx_transcriber = None

def get_whisperx_transcriber(model_size="base", device=None):
    """Get or create WhisperX transcriber instance"""
    global _whisperx_transcriber
    
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    
    if _whisperx_transcriber is None:
        _whisperx_transcriber = WhisperXTranscriber(model_size, device)
    
    return _whisperx_transcriber