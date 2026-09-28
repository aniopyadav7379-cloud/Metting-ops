"""
Unified Transcription Service
Manages dynamic model switching and unified transcription interface.

This is the legacy NPU-first STT path used on the UC-1 appliance build. On
hosts without an NPU (the cloud build on bigboy) it stays inert: upload
transcription routes through ProviderRegistry.get_stt() to whisper-server, and
the live-recording path that consumed this service needs a local mic that the
cloud build does not have. The DISABLE_LOCAL_AUDIO env gate (set by
deploy/bigboy/.env.bigboy) skips initialization entirely so the NPU runtime
is not loaded and no startup errors fire.
"""
import logging
import asyncio
import os
import time
from typing import Dict, List, Optional, Any, Union
from datetime import datetime
import numpy as np

from services.settings_manager import settings_manager
from services.stt_model_manager import stt_model_manager

from services.performance_metrics_service import performance_metrics_service

logger = logging.getLogger(__name__)


def _local_audio_disabled() -> bool:
    """Return True when the host opted out of local audio + NPU initialization."""
    val = os.getenv("DISABLE_LOCAL_AUDIO", "").strip().lower()
    return val in ("1", "true", "yes", "on")

# Working transcriber not needed - NPU transcription is available
WORKING_TRANSCRIBER_AVAILABLE = False

class TranscriptionResult:
    """Standardized transcription result"""
    
    def __init__(self, 
                 text: str, 
                 confidence: float = 0.0,
                 start_time: Optional[float] = None,
                 end_time: Optional[float] = None,
                 speaker_id: Optional[str] = None,
                 language: Optional[str] = None,
                 segments: Optional[List[Dict]] = None):
        self.text = text
        self.confidence = confidence
        self.start_time = start_time
        self.end_time = end_time
        self.speaker_id = speaker_id
        self.language = language
        self.segments = segments or []
        
    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "confidence": self.confidence,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "speaker_id": self.speaker_id,
            "language": self.language,
            "segments": self.segments
        }

class TranscriptionService:
    """Unified transcription service with dynamic model switching"""
    
    def __init__(self):
        self.current_engine = None
        self.current_model_id = None
        self.is_ready = False
        self._models = {}

        if _local_audio_disabled():
            # Cloud / non-NPU build. Upload transcription is handled by
            # ProviderRegistry.get_stt(); leave this service inert so we don't
            # try to load NPU kernels on hardware that doesn't have them.
            logger.info(
                "Local transcription service skipped (DISABLE_LOCAL_AUDIO set); "
                "STT routes through ProviderRegistry.get_stt() instead."
            )
            return

        # Initialize with default model
        logger.info("Attempting to initialize default model...")
        self._initialize_default_model()
        logger.info(f"Default model initialization complete. is_ready: {self.is_ready}, current_model_id: {self.current_model_id}")
    
    def _initialize_default_model(self):
        """Initialize with the default STT model"""
        try:
            current_model = settings_manager.get_setting("ai.whisper_model")
            if not current_model:
                current_model = "whisperx-npu"  # Default to WhisperX NPU
            # Map settings values to actual model IDs
            model_mapping = {
                "whisper-onnx-npu": "whisperx-npu",
                "whisperx": "whisperx-npu",
                "base": "whisperx-npu",  # Default base to WhisperX
                "whisper-base": "whisperx-npu"  # Map whisper-base to WhisperX
            }
            current_model = model_mapping.get(current_model, current_model)
            self.switch_model(current_model)
        except Exception as e:
            logger.error(f"Failed to initialize default model: {e}")
            # Fall back to basic ONNX model
            self._initialize_fallback_model()
    
    def _initialize_fallback_model(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def _initialize_npu_runtime(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def switch_model(self, model_id: str) -> bool:
        """Switch to a different STT model"""
        try:
            if model_id == self.current_model_id and self.is_ready:
                logger.info(f"Model {model_id} already active")
                return True
            
            logger.info(f"Attempting to switch to model: {model_id}")
            
            # Get model info
            model_info = stt_model_manager.NPU_MODELS.get(model_id)
            if not model_info:
                logger.error(f"Unknown model: {model_id}")
                return False
            
            # Clean up current engine
            if self.current_engine:
                logger.info("Cleaning up current engine...")
                try:
                    if hasattr(self.current_engine, 'cleanup'):
                        self.current_engine.cleanup()
                    self.current_engine = None # Ensure it's set to None after cleanup
                except Exception as e:
                    logger.warning(f"Error cleaning up previous engine: {e}")
            
            # Initialize new engine based on model type
            engine_type = model_info.get("source", "")
            logger.info(f"Initializing new engine for model type: {engine_type}")
            if model_id == "npu-runtime":
                self.current_engine = self._initialize_npu_runtime()
            elif model_id == "whisperx-npu-unified":
                self.current_engine = self._initialize_whisperx_unified()
            elif model_id == "whisperx-npu":
                self.current_engine = self._initialize_whisperx_npu()
            elif model_id == "whisper-onnx-npu":
                self.current_engine = self._initialize_onnx_npu()
            elif model_id == "diarization-npu":
                self.current_engine = self._initialize_diarization_npu()
            elif model_id in ["whisper-base", "whisper-small", "whisper-medium"]:
                # Use NPU for whisper models if available
                self.current_engine = self._initialize_npu_whisper(model_id)
            elif model_id == "pyannote-diarization":
                self.current_engine = self._initialize_pyannote()
            else:
                logger.error(f"Unsupported model: {model_id}")
                return False
            
            if self.current_engine:
                self.current_model_id = model_id
                self.is_ready = True
                logger.info(f"New engine initialized. is_ready: {self.is_ready}, current_model_id: {self.current_model_id}")
                
                # Update settings
                settings_manager.update_setting("ai.whisper_model", model_id)
                
                logger.info(f"✅ Successfully switched to {model_info['name']}")
                return True
            else:
                logger.error(f"Failed to initialize {model_id}")
                return False
                
        except Exception as e:
            logger.error(f"Error switching to model {model_id}: {e}")
            return False
    
    def _initialize_npu_whisper(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def _initialize_whisperx_unified(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def _initialize_whisperx_npu(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def _initialize_onnx_npu(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def _initialize_diarization_npu(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def _initialize_onnx_cpu(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def _initialize_pyannote(self, *args, **kwargs):
        """Removed with the NPU/appliance modules (see Meeting-Ops-UC1)."""
        return None

    def process_audio_chunk(self, audio_data: bytes, session_id: Optional[str] = None) -> Optional[TranscriptionResult]:
        """Process audio chunk and return transcription"""
        # Use working transcriber if available
        if WORKING_TRANSCRIBER_AVAILABLE:
            try:
                # Convert bytes to numpy array
                audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
                
                # Use working transcriber
                transcriber = WorkingTranscriber()
                result = transcriber.transcribe_chunk(audio_array)
                
                if result and result.get("text"):
                    return TranscriptionResult(
                        text=result["text"],
                        confidence=0.95,
                        start_time=time.time(),
                        end_time=time.time()
                    )
            except Exception as e:
                logger.error(f"Working transcriber error: {e}")
        
        # Fall back to original logic
        if not self.is_ready or not self.current_engine:
            logger.warning("Transcription service not ready")
            return None
        
        # Performance tracking
        start_time = time.time()
        audio_duration = 0
        words_transcribed = 0
        speakers_detected = 0
        confidence_avg = 0.0
        
        try:
            # Convert bytes to numpy array if needed
            if isinstance(audio_data, bytes):
                audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            else:
                audio_array = audio_data
            
            logger.debug(f"Processing audio chunk: shape={audio_array.shape}, dtype={audio_array.dtype}")

            # Calculate audio duration
            audio_duration = len(audio_array) / 16000  # Assuming 16kHz sample rate
            logger.debug(f"Audio duration: {audio_duration:.3f}s")
            
            # Call the appropriate transcription method based on engine type
            if hasattr(self.current_engine, 'transcribe_chunk'):
                logger.debug("Calling transcribe_chunk on current_engine")
                result = self.current_engine.transcribe_chunk(audio_array)
            elif hasattr(self.current_engine, 'transcribe_chunk'):
                logger.debug("Calling transcribe on current_engine")
                result = self.current_engine.transcribe_chunk(audio_array)
            else:
                logger.error("Current engine doesn't support transcription")
                return None
            
            logger.debug(f"Raw transcription result: {result}")

            # Process result and extract metrics
            if result:
                standardized_result = self._standardize_result(result)
                logger.debug(f"Standardized result: {standardized_result.to_dict()}")
                
                # Extract performance metrics
                if standardized_result.text:
                    words_transcribed = len(standardized_result.text.split())
                confidence_avg = standardized_result.confidence
                
                # Count speakers if segments available
                if standardized_result.segments:
                    unique_speakers = set()
                    for segment in standardized_result.segments:
                        if isinstance(segment, dict) and 'speaker' in segment:
                            unique_speakers.add(segment['speaker'])
                        elif hasattr(segment, 'get') and segment.get('speaker_id'):
                            unique_speakers.add(segment.get('speaker_id'))
                    speakers_detected = len(unique_speakers)
                
                # Record performance metrics
                processing_time = time.time() - start_time
                model_info = self.get_current_model_info()
                
                performance_metrics_service.record_model_performance(
                    session_id=session_id or "unknown",
                    model_id=self.current_model_id or "unknown",
                    model_name=model_info.get("name", "Unknown Model"),
                    audio_duration=audio_duration,
                    processing_time=processing_time,
                    npu_accelerated="npu" in (self.current_model_id or "").lower(),
                    confidence_avg=confidence_avg,
                    words_transcribed=words_transcribed,
                    speakers_detected=speakers_detected
                )
                
                logger.debug(f"📊 Metrics recorded: {words_transcribed} words, {processing_time:.3f}s processing, RTF: {processing_time/audio_duration:.3f}")
                
                return standardized_result
            
        except Exception as e:
            logger.error(f"Error processing audio chunk: {e}")
            
            # Record failed performance metric
            processing_time = time.time() - start_time
            if self.current_model_id and audio_duration > 0:
                model_info = self.get_current_model_info()
                performance_metrics_service.record_model_performance(
                    session_id=session_id or "unknown",
                    model_id=self.current_model_id,
                    model_name=model_info.get("name", "Unknown Model"),
                    audio_duration=audio_duration,
                    processing_time=processing_time,
                    npu_accelerated="npu" in self.current_model_id.lower(),
                    confidence_avg=0.0,  # Failed transcription
                    words_transcribed=0,
                    speakers_detected=0
                )
        
        return None
    
    def process_audio_file(self, file_path: str) -> Optional[TranscriptionResult]:
        """Process complete audio file"""
        if not self.is_ready or not self.current_engine:
            logger.warning("Transcription service not ready")
            return None
        
        try:
            if hasattr(self.current_engine, 'transcribe_file'):
                result = self.current_engine.transcribe_file(file_path)
            elif hasattr(self.current_engine, 'transcribe_chunk'):
                # Load audio file and process
                import librosa
                audio, sr = librosa.load(file_path, sr=16000)
                result = self.current_engine.transcribe(audio)
            else:
                logger.error("Current engine doesn't support file transcription")
                return None
            
            if result:
                return self._standardize_result(result)
                
        except Exception as e:
            logger.error(f"Error processing audio file {file_path}: {e}")
        
        return None
    
    def _standardize_result(self, raw_result: Any) -> TranscriptionResult:
        """Convert engine-specific result to standardized format"""
        try:
            # Handle different result formats from different engines
            if isinstance(raw_result, dict):
                return TranscriptionResult(
                    text=raw_result.get("text", ""),
                    confidence=raw_result.get("confidence", 0.0),
                    start_time=raw_result.get("start_time"),
                    end_time=raw_result.get("end_time"),
                    speaker_id=raw_result.get("speaker_id"),
                    language=raw_result.get("language", "en"),
                    segments=raw_result.get("segments", [])
                )
            elif isinstance(raw_result, str):
                return TranscriptionResult(text=raw_result, confidence=0.9)
            elif hasattr(raw_result, 'text'):
                # Handle Whisper result objects
                return TranscriptionResult(
                    text=getattr(raw_result, 'text', ""),
                    confidence=0.9,  # Default confidence for Whisper
                    segments=getattr(raw_result, 'segments', [])
                )
            else:
                logger.warning(f"Unknown result format: {type(raw_result)}")
                return TranscriptionResult(text=str(raw_result), confidence=0.5)
                
        except Exception as e:
            logger.error(f"Error standardizing result: {e}")
            return TranscriptionResult(text="", confidence=0.0)
    
    def get_current_model_info(self) -> Dict[str, Any]:
        """Get information about the current model"""
        if not self.current_model_id:
            return {}
        
        model_info = stt_model_manager.NPU_MODELS.get(self.current_model_id, {})
        return {
            "model_id": self.current_model_id,
            "name": model_info.get("name", "Unknown"),
            "description": model_info.get("description", ""),
            "speed": model_info.get("speed", "Unknown"),
            "accuracy": model_info.get("accuracy", "Unknown"),
            "is_ready": self.is_ready,
            "engine_type": type(self.current_engine).__name__ if self.current_engine else None
        }
    
    def get_available_models(self) -> List[Dict[str, Any]]:
        """Get list of available models"""
        return stt_model_manager.list_available_models()
    
    def benchmark_current_model(self) -> Dict[str, Any]:
        """Benchmark the current model"""
        if not self.is_ready or not self.current_model_id:
            return {"error": "No model active"}
        
        return stt_model_manager.benchmark_model(self.current_model_id)
    
    def cleanup(self):
        """Clean up resources"""
        if self.current_engine and hasattr(self.current_engine, 'cleanup'):
            try:
                self.current_engine.cleanup()
            except Exception as e:
                logger.error(f"Error during cleanup: {e}")
        
        self.current_engine = None
        self.current_model_id = None
        self.is_ready = False
    
    def transcribe_file(self, file_path: str) -> Optional[dict]:
        """Simple alias for process_audio_file that returns dict format"""
        result = self.process_audio_file(file_path)
        if result:
            return {
                "text": result.text,
                "segments": getattr(result, 'segments', []),
                "confidence": getattr(result, 'confidence', 0.95)
            }
        return None

# Global transcription service instance
transcription_service = TranscriptionService()