#!/usr/bin/env python3
"""
Real Whisper Transcriber - Uses WhisperX/Whisper with CPU/GPU fallback
No mock data - actual speech-to-text transcription
"""

import numpy as np
import logging
import torch
import whisperx
import gc
from typing import Dict, Optional
import warnings
warnings.filterwarnings("ignore", category=UserWarning)

logger = logging.getLogger(__name__)

class RealWhisperTranscriber:
    """Real Whisper transcriber with CPU/GPU support"""
    
    def __init__(self, model_size="base"):
        self.model_size = model_size
        self.model = None
        self.device = None
        self.compute_type = None
        self.is_ready = False
        
        logger.info(f"🎤 Initializing Real Whisper Transcriber ({model_size})")
        self._initialize()
    
    def _initialize(self):
        """Initialize Whisper with best available device"""
        try:
            # Detect best available device
            if torch.cuda.is_available():
                self.device = "cuda"
                self.compute_type = "float16"
                logger.info("🎮 CUDA GPU detected - using GPU acceleration")
            else:
                self.device = "cpu"
                self.compute_type = "int8"  # Use INT8 for faster CPU inference
                logger.info("💻 Using CPU with INT8 optimization")
            
            # Load WhisperX model
            logger.info(f"Loading WhisperX model ({self.model_size})...")
            self.model = whisperx.load_model(
                self.model_size,
                self.device,
                compute_type=self.compute_type,
                language="en"  # Can be made configurable
            )
            
            self.is_ready = True
            logger.info(f"✅ WhisperX ready on {self.device.upper()}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to initialize WhisperX: {e}")
            
            # Fallback to standard Whisper
            try:
                import whisper
                logger.info("Falling back to standard Whisper...")
                self.model = whisper.load_model(self.model_size)
                self.device = "cpu"
                self.is_ready = True
                logger.info("✅ Standard Whisper ready on CPU")
                return True
            except Exception as e2:
                logger.error(f"❌ Failed to initialize standard Whisper: {e2}")
                return False
    
    def transcribe_chunk(self, audio_chunk: np.ndarray) -> Dict:
        """Transcribe audio chunk using real Whisper"""
        
        if not self.is_ready or self.model is None:
            return {
                "text": "",
                "error": "Whisper not initialized",
                "confidence": 0.0
            }
        
        try:
            # Ensure audio is float32 and normalized
            if audio_chunk.dtype != np.float32:
                audio_chunk = audio_chunk.astype(np.float32)
            
            # Normalize if needed
            if np.abs(audio_chunk).max() > 1.0:
                audio_chunk = audio_chunk / 32768.0
            
            # Check if there's meaningful audio
            if np.abs(audio_chunk).max() < 0.001:
                return {
                    "text": "",
                    "confidence": 0.0,
                    "engine": f"whisperx-{self.device}",
                    "no_speech": True
                }
            
            # Run transcription
            result = self.model.transcribe(
                audio_chunk,
                batch_size=1 if hasattr(self.model, 'transcribe') else None
            )
            
            # Extract text from result
            if isinstance(result, dict):
                # WhisperX format
                segments = result.get("segments", [])
                if segments:
                    text = " ".join(seg.get("text", "").strip() for seg in segments)
                else:
                    text = result.get("text", "").strip()
                
                # Calculate average confidence if available
                confidence = 0.9  # Default high confidence for real transcription
                if segments:
                    confidences = [seg.get("confidence", 0.9) for seg in segments if "confidence" in seg]
                    if confidences:
                        confidence = sum(confidences) / len(confidences)
            else:
                # Simple text result
                text = str(result).strip()
                confidence = 0.9
            
            return {
                "text": text,
                "confidence": float(confidence),
                "language": result.get("language", "en") if isinstance(result, dict) else "en",
                "engine": f"whisperx-{self.device}",
                "device": self.device,
                "compute_type": self.compute_type
            }
            
        except Exception as e:
            logger.error(f"❌ Transcription error: {e}")
            return {
                "text": "",
                "error": str(e),
                "confidence": 0.0,
                "engine": f"whisperx-{self.device}"
            }
    
    def cleanup(self):
        """Clean up resources"""
        if self.model:
            del self.model
            gc.collect()
            if self.device == "cuda":
                torch.cuda.empty_cache()
        self.is_ready = False
        logger.info("🧹 Cleaned up Whisper resources")

# Global instance
_real_transcriber = None

def get_real_whisper_transcriber(model_size="base"):
    """Get or create real Whisper transcriber instance"""
    global _real_transcriber
    if _real_transcriber is None:
        _real_transcriber = RealWhisperTranscriber(model_size)
    return _real_transcriber