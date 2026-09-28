"""
Transcription Configuration
Controls the behavior of the transcription system
"""

import os
from typing import Dict, Any

class TranscriptionConfig:
    """
    Configuration for transcription-first mode
    """
    
    # Primary mode (transcription_first, audio_first, both)
    MODE = os.getenv("TRANSCRIPTION_MODE", "transcription_first")
    
    # Buffer settings
    BUFFER_DURATION_MINUTES = int(os.getenv("BUFFER_DURATION_MINUTES", "60"))
    BUFFER_TYPE = os.getenv("BUFFER_TYPE", "redis")  # redis or memory
    
    # Audio settings
    RECORD_AUDIO_DEFAULT = os.getenv("RECORD_AUDIO_DEFAULT", "false").lower() == "true"
    AUDIO_FORMAT = os.getenv("AUDIO_FORMAT", "wav")  # wav, mkv, opus
    AUDIO_COMPRESSION = os.getenv("AUDIO_COMPRESSION", "none")  # none, opus, aac
    
    # Storage settings
    AUTO_SAVE_TRANSCRIPTS = os.getenv("AUTO_SAVE_TRANSCRIPTS", "false").lower() == "true"
    AUTO_SAVE_INTERVAL_MINUTES = int(os.getenv("AUTO_SAVE_INTERVAL_MINUTES", "30"))
    
    # Transcription settings
    CHUNK_DURATION_SECONDS = int(os.getenv("CHUNK_DURATION_SECONDS", "5"))
    USE_NPU = os.getenv("USE_NPU", "true").lower() == "true"
    
    # Storage comparison (for reference)
    TRANSCRIPT_SIZE_KB_PER_MINUTE = 3  # ~3KB per minute of speech
    AUDIO_SIZE_MB_PER_MINUTE = 5.3  # ~5.3MB per minute at 44.1kHz WAV
    
    @classmethod
    def get_config(cls) -> Dict[str, Any]:
        """Get current configuration as dictionary"""
        return {
            "mode": cls.MODE,
            "buffer": {
                "duration_minutes": cls.BUFFER_DURATION_MINUTES,
                "type": cls.BUFFER_TYPE
            },
            "audio": {
                "record_by_default": cls.RECORD_AUDIO_DEFAULT,
                "format": cls.AUDIO_FORMAT,
                "compression": cls.AUDIO_COMPRESSION
            },
            "storage": {
                "auto_save": cls.AUTO_SAVE_TRANSCRIPTS,
                "auto_save_interval": cls.AUTO_SAVE_INTERVAL_MINUTES,
                "transcript_kb_per_min": cls.TRANSCRIPT_SIZE_KB_PER_MINUTE,
                "audio_mb_per_min": cls.AUDIO_SIZE_MB_PER_MINUTE,
                "savings_ratio": int(cls.AUDIO_SIZE_MB_PER_MINUTE * 1024 / cls.TRANSCRIPT_SIZE_KB_PER_MINUTE)
            },
            "transcription": {
                "chunk_duration": cls.CHUNK_DURATION_SECONDS,
                "use_npu": cls.USE_NPU
            }
        }
    
    @classmethod
    def is_transcription_first(cls) -> bool:
        """Check if we're in transcription-first mode"""
        return cls.MODE == "transcription_first"
    
    @classmethod
    def should_record_audio(cls) -> bool:
        """Check if audio should be recorded by default"""
        return cls.RECORD_AUDIO_DEFAULT or cls.MODE == "audio_first"

# Export singleton
transcription_config = TranscriptionConfig()