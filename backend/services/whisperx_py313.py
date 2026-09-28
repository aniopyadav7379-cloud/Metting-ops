"""
WhisperX functionality for Python 3.13

This module provides WhisperX-compatible transcription and diarization
using Python 3.13 compatible libraries.
"""

import os
import logging
from typing import Dict, List, Optional, Any
import numpy as np
import torch
import torchaudio
from faster_whisper import WhisperModel
from pyannote.audio import Pipeline
from pyannote.audio.pipelines.utils.hook import ProgressHook
import pandas as pd

logger = logging.getLogger(__name__)


class WhisperXTranscriber:
    """WhisperX-compatible transcriber for Python 3.13"""
    
    def __init__(
        self,
        model_size: str = "base",
        device: str = "cpu",
        compute_type: str = "int8",
        language: Optional[str] = None,
        diarize: bool = True,
        min_speakers: int = 1,
        max_speakers: int = 10,
    ):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.language = language
        self.diarize = diarize
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers
        
        # Initialize Whisper model
        logger.info(f"Loading Whisper {model_size} model...")
        self.whisper_model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type
        )
        
        # Initialize diarization pipeline if requested
        self.diarization_pipeline = None
        if diarize:
            try:
                logger.info("Loading speaker diarization pipeline...")
                self.diarization_pipeline = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1",
                    use_auth_token=os.getenv("HUGGINGFACE_TOKEN")
                )
                if device == "cuda" and torch.cuda.is_available():
                    self.diarization_pipeline.to(torch.device("cuda"))
            except Exception as e:
                logger.warning(f"Failed to load diarization pipeline: {e}")
                self.diarize = False
    
    def transcribe(
        self,
        audio_path: str,
        batch_size: int = 16,
        word_timestamps: bool = True,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Transcribe audio file with optional speaker diarization
        
        Args:
            audio_path: Path to audio file
            batch_size: Batch size for transcription
            word_timestamps: Whether to include word-level timestamps
            
        Returns:
            Dictionary containing transcription results
        """
        # Transcribe with Whisper
        logger.info(f"Transcribing {audio_path}...")
        segments, info = self.whisper_model.transcribe(
            audio_path,
            language=self.language,
            word_timestamps=word_timestamps,
            vad_filter=False,  # Disable VAD - it's too aggressive
            **kwargs
        )
        
        # Convert segments to list
        segments_list = list(segments)
        
        # Format results
        result = {
            "segments": [],
            "language": info.language,
            "duration": info.duration,
        }
        
        for segment in segments_list:
            seg_dict = {
                "start": segment.start,
                "end": segment.end,
                "text": segment.text.strip(),
            }
            
            if word_timestamps and hasattr(segment, 'words'):
                seg_dict["words"] = [
                    {
                        "start": word.start,
                        "end": word.end,
                        "word": word.word.strip(),
                        "probability": getattr(word, 'probability', 1.0)
                    }
                    for word in segment.words
                ]
            
            result["segments"].append(seg_dict)
        
        # Perform speaker diarization if enabled
        if self.diarize and self.diarization_pipeline:
            try:
                logger.info("Performing speaker diarization...")
                diarization = self.diarization_pipeline(
                    audio_path,
                    min_speakers=self.min_speakers,
                    max_speakers=self.max_speakers
                )
                
                # Assign speakers to segments
                result["segments"] = self._assign_speakers(
                    result["segments"],
                    diarization
                )
                
                # Add speaker info
                result["speakers"] = self._get_speaker_info(diarization)
                
            except Exception as e:
                logger.error(f"Diarization failed: {e}")
        
        return result
    
    def _assign_speakers(self, segments: List[Dict], diarization) -> List[Dict]:
        """Assign speaker labels to transcription segments"""
        for segment in segments:
            start_time = segment["start"]
            end_time = segment["end"]
            
            # Find speaker for this segment
            speakers = []
            for turn, _, speaker in diarization.itertracks(yield_label=True):
                if turn.start <= start_time < turn.end or turn.start < end_time <= turn.end:
                    speakers.append(speaker)
            
            # Assign most common speaker
            if speakers:
                segment["speaker"] = max(set(speakers), key=speakers.count)
            else:
                segment["speaker"] = "UNKNOWN"
        
        return segments
    
    def _get_speaker_info(self, diarization) -> Dict[str, Dict]:
        """Extract speaker information from diarization"""
        speakers = {}
        
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            if speaker not in speakers:
                speakers[speaker] = {
                    "speaking_time": 0.0,
                    "num_turns": 0
                }
            
            speakers[speaker]["speaking_time"] += turn.duration
            speakers[speaker]["num_turns"] += 1
        
        return speakers


def load_model(
    model_name: str = "base",
    device: str = "cpu",
    compute_type: str = "int8",
    language: Optional[str] = None,
    **kwargs
) -> WhisperXTranscriber:
    """
    Load WhisperX model (compatibility function)
    
    Args:
        model_name: Whisper model size
        device: Device to use (cpu/cuda)
        compute_type: Computation type (int8/float16/float32)
        language: Language code
        
    Returns:
        WhisperXTranscriber instance
    """
    return WhisperXTranscriber(
        model_size=model_name,
        device=device,
        compute_type=compute_type,
        language=language,
        **kwargs
    )


def load_align_model(language_code: str, device: str = "cpu"):
    """Placeholder for alignment model (not implemented in this version)"""
    logger.warning("Alignment model not implemented in Python 3.13 version")
    return None


def align(
    segments: List[Dict],
    align_model,
    metadata,
    audio_path: str,
    device: str = "cpu",
    **kwargs
) -> Dict[str, Any]:
    """Placeholder for alignment (segments already have word timestamps)"""
    return {"segments": segments}


# Make it importable like whisperx
__all__ = ["load_model", "load_align_model", "align", "WhisperXTranscriber"]