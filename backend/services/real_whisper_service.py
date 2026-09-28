"""
LEGACY Whisper transcription service (UC-1 appliance line).

Not the live path: this product transcribes with Parakeet (browser + server).
The "220x speedup" previously claimed here was a hardcoded constant, never measured.
"""

import logging
import time
import os
from typing import Dict, Optional


logger = logging.getLogger(__name__)

class RealWhisperService:
    """CPU WhisperX transcription (upload / satellite / remote-audio fallback path)."""

    def __init__(self, model_name: str = "large-v3"):
        self.model_name = model_name
        self.engine = None
        self.is_initialized = False
        
    def initialize(self):
        """Load the CPU WhisperX model."""
        try:
            from services.whisperx_py313 import WhisperXTranscriber
            self.engine = WhisperXTranscriber(
                model_size=self.model_name,
                device="cpu",
                compute_type="int8",
                diarize=True,
            )
            self.is_initialized = True
            logger.info(f"CPU WhisperX model '{self.model_name}' loaded")
            return True
        except Exception as e:
            logger.error(f"Failed to load WhisperX model: {e}")
            return False

    def transcribe_file(self, audio_path: str, **kwargs) -> Dict:
        """Transcribe an audio file with CPU WhisperX."""
        if not self.is_initialized:
            if not self.initialize():
                logger.error("Failed to initialize WhisperX model")
                return self._get_error_result()
                
        if not os.path.exists(audio_path):
            logger.error(f"Audio file not found: {audio_path}")
            return self._get_error_result()
            
        try:
            start_time = time.time()
            logger.info(f"Transcribing: {audio_path}")
            
            # Extract diarization flag from kwargs
            diarize = kwargs.get("diarize", True)
            
            result = self.engine.transcribe(audio_path, word_timestamps=True)
            
            processing_time = time.time() - start_time
            
            # Get audio duration for RTF calculation
            audio_duration = result.get("duration", 0)
            if audio_duration == 0:
                audio_duration = self._get_audio_duration(audio_path)
            
            rtf = processing_time / audio_duration if audio_duration > 0 else 0
            speedup = 1.0 / rtf if rtf > 0 else 0
            
            logger.info(f"Transcription complete in {processing_time:.2f}s")
            logger.info(f"⚡ Performance: RTF={rtf:.4f}, Speedup={speedup:.1f}x")
            
            # Format result for backend compatibility
            formatted_result = {
                "text": result.get("text", ""),
                "segments": self._format_segments(result.get("segments", [])),
                "language": result.get("language", "en"),
                "duration": processing_time,
                "audio_duration": audio_duration,
                "rtf": rtf,
                "speedup": speedup,
                "model": self.model_name,
                "success": True
            }
            
            return formatted_result
            
        except Exception as e:
            logger.error(f"Transcription failed: {e}", exc_info=True)
            return self._get_error_result()
            
    def _format_segments(self, segments):
        """Format WhisperX segments for backend compatibility."""
        formatted = []
        for seg in segments:
            formatted_seg = {
                "start": seg.get("start", 0),
                "end": seg.get("end", 0),
                "text": seg.get("text", "").strip(),
                "confidence": seg.get("confidence", 0.95),
                "speaker": seg.get("speaker", "SPEAKER_00")
            }
            
            # Add word-level timestamps if available
            if "words" in seg:
                formatted_seg["words"] = []
                for w in seg["words"]:
                    # Handle both dict and object types
                    if hasattr(w, 'word'):  # faster-whisper Word object
                        word_data = {
                            "word": getattr(w, 'word', ''),
                            "start": getattr(w, 'start', 0),
                            "end": getattr(w, 'end', 0),
                            "confidence": getattr(w, 'probability', 0.95)
                        }
                    else:  # dict format
                        word_data = {
                            "word": w.get("word", ""),
                            "start": w.get("start", 0),
                            "end": w.get("end", 0),
                            "confidence": w.get("confidence", w.get("probability", 0.95))
                        }
                    formatted_seg["words"].append(word_data)
                
            formatted.append(formatted_seg)
            
        return formatted
        
    def _get_error_result(self):
        """Return error result format"""
        return {
            "text": "[Transcription failed]",
            "segments": [],
            "success": False,
            "model": self.model_name,
        }
    
    def _get_audio_duration(self, audio_path: str) -> float:
        """Get duration of audio file in seconds"""
        try:
            import wave
            with wave.open(audio_path, 'rb') as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                duration = frames / float(rate)
                return duration
        except Exception:
            return 0.0
    
    def get_status(self) -> Dict:
        """Engine status."""
        return {
            "initialized": self.is_initialized,
            "model": self.model_name,
            "engine": "WhisperX CPU",
        }


# Global instance
real_whisper_service = RealWhisperService(model_name="large-v3")