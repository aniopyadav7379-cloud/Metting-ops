"""
NPU Whisper Transcriber - Bridge to existing NPU implementation
"""

import logging
import numpy as np
from typing import Dict, Optional, List
import time
from datetime import datetime

logger = logging.getLogger(__name__)

class NPUWhisperTranscriber:
    """Wrapper class for NPU WhisperX transcription"""
    
    def __init__(self):
        self.engine = None
        self.is_ready = False
        self.model_name = "whisper-base"
        
    def initialize(self):
        """Initialize the NPU transcription engine"""
        try:
            # Import the existing NPU transcription service
            from services.whisperx_npu_service import WhisperXNPUService
            
            logger.info("Initializing WhisperX NPU engine...")
            self.engine = WhisperXNPUService()
            self.is_ready = True
            logger.info("✅ NPU Whisper Transcriber initialized successfully")
            
        except ImportError as e:
            logger.error(f"Failed to import NPU service: {e}")
            # Fallback to mock implementation
            self.is_ready = True
            self.engine = None
            logger.warning("⚠️ Using mock NPU transcriber")
            
    def transcribe_audio(self, audio_data: bytes) -> Optional[Dict]:
        """Transcribe audio using NPU acceleration"""
        if not self.is_ready:
            logger.warning("NPU transcriber not ready")
            return None
            
        try:
            if self.engine:
                # Use real NPU engine
                start_time = time.time()
                
                # Convert bytes to numpy array (assume 16-bit PCM, 44.1kHz)
                audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
                
                # Call existing NPU transcription
                result = self.engine.transcribe_audio_data(audio_array, sample_rate=44100)
                
                processing_time = time.time() - start_time
                
                if result and result.get('text', '').strip():
                    return {
                        'text': result['text'].strip(),
                        'confidence': result.get('confidence', 0.95),
                        'speaker': result.get('speaker', 'Speaker'),
                        'processing_time_ms': processing_time * 1000,
                        'npu_active': True
                    }
                    
            else:
                # Mock implementation for testing
                if len(audio_data) > 8000:  # Only transcribe if we have enough audio
                    return {
                        'text': f"[NPU Mock] Test transcription at {datetime.now().strftime('%H:%M:%S')}",
                        'confidence': 0.95,
                        'speaker': 'Speaker',
                        'processing_time_ms': 15.0,
                        'npu_active': False
                    }
                    
        except Exception as e:
            logger.error(f"NPU transcription error: {e}")
            
        return None
        
    def is_available(self) -> bool:
        """Check if NPU transcriber is available"""
        return self.is_ready
        
    def get_model_info(self) -> Dict:
        """Get model information"""
        return {
            'model': self.model_name,
            'engine': 'WhisperX-NPU',
            'acceleration': 'AMD NPU' if self.engine else 'Mock',
            'ready': self.is_ready
        }