"""
Live Recording Transcription Service
Processes chunks of audio during recording for real-time transcription.
Primary: whisper.cpp server (Vulkan iGPU, large-v3-turbo, ~9.6x realtime)
Fallback: transcription_service (CPU faster-whisper)
"""

import asyncio
import logging
import time
import os
import json
import tempfile
from pathlib import Path
from typing import Optional, Dict, List
import numpy as np
import soundfile as sf
import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Redis URL from environment (default matches docker-compose port 6381)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6381")

# Known Whisper hallucination patterns (case-insensitive, stripped)
# These appear when Whisper processes silence or near-silence
_HALLUCINATION_PATTERNS = {
    "thank you", "thank you.", "thank you!", "thank you for watching",
    "thank you for watching.", "thanks for watching", "thanks for watching.",
    "thanks", "thanks.", "bye", "bye.", "bye bye", "bye bye.",
    "goodbye", "goodbye.", "you", "you.", "the end", "the end.",
    "subscribe", "subscribe.", "please subscribe",
    "like and subscribe", "see you next time",
    "subtitles by", "subtitles made by", "amara.org",
}


def _is_hallucination(text: str) -> bool:
    """Check if transcribed text is a known Whisper hallucination."""
    cleaned = text.strip().lower().rstrip("!.,?")
    if cleaned in _HALLUCINATION_PATTERNS:
        return True
    # Catch repeated single-word outputs
    words = cleaned.split()
    if len(words) <= 3 and len(set(words)) == 1:
        return True
    # Catch dots-only output (Whisper outputs "......" on amplified noise)
    if all(c in '.… ' for c in text.strip()):
        return True
    return False


def _chunk_has_speech(audio: np.ndarray, threshold_db: float = -75.0) -> bool:
    """Check if audio chunk has enough energy to contain speech.
    Returns False for near-digital-silence to avoid sending to Whisper.
    Threshold is very permissive (-75dB) since USB mics often output low levels
    (typical speech at -50 to -67dB RMS). Only filters true digital silence.
    """
    if len(audio) == 0:
        return False
    rms = np.sqrt(np.mean(audio ** 2))
    if rms < 1e-10:
        return False
    db = 20 * np.log10(rms)
    return db > threshold_db

class LiveRecordingTranscriptionService:
    """
    Service that monitors an active recording and transcribes chunks
    in real-time for live transcription display.
    Primary: whisper.cpp HTTP server (Vulkan iGPU) on port 8178
    Fallback: local transcription_service (CPU faster-whisper)
    """

    def __init__(self):
        self.is_active = False
        self.current_file = None
        self.last_position = 0
        self.transcription_task = None
        self.redis_client = None
        self.transcript_buffer = []  # Buffer for meeting notes generation
        self.session_word_counts = {}  # Track word counts per session
        self.triggered_intervals = {}  # Track triggered summary intervals

        # Auto-stop on prolonged silence
        self.consecutive_silent_chunks = 0
        self.max_silent_chunks = 20  # 20 chunks * 15s = 5 minutes of silence
        self.auto_stop_triggered = False

    async def start_monitoring(self, audio_file: str, session_id: str):
        """Start monitoring a recording file for live transcription"""
        self.current_file = audio_file
        self.session_id = session_id
        self.last_position = 0
        self.is_active = True
        self.transcript_buffer = []
        self.consecutive_silent_chunks = 0
        self.auto_stop_triggered = False

        logger.info(f"Starting live transcription monitoring")
        logger.info(f"   File: {audio_file}")
        logger.info(f"   Session: {session_id}")
        logger.info(f"   File exists: {os.path.exists(audio_file)}")

        # Start the monitoring task
        self.transcription_task = asyncio.create_task(
            self._monitor_and_transcribe()
        )
        logger.info(f"Live transcription task started")

    async def stop_monitoring(self):
        """Stop monitoring"""
        self.is_active = False
        if self.transcription_task:
            self.transcription_task.cancel()
            try:
                await self.transcription_task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped live transcription monitoring")

    async def _monitor_and_transcribe(self):
        """Monitor recording file and transcribe new chunks via whisper-server (Vulkan iGPU)"""
        chunk_duration = 15  # 15-second chunks for good accuracy
        chunk_overlap = 2   # 2-second overlap for continuity
        recording_sample_rate = 44100  # Recording at 44.1kHz
        target_sample_rate = 16000     # Whisper needs 16kHz
        chunk_samples = chunk_duration * recording_sample_rate
        overlap_samples = chunk_overlap * recording_sample_rate
        min_chunk_duration = 3  # Minimum chunk size to avoid bad transcriptions

        # Try whisper-server (Vulkan iGPU, ~9.6x realtime) first, fallback to CPU
        from services.whisper_server_client import whisper_server_client
        use_whisper_server = whisper_server_client.is_available()

        if use_whisper_server:
            logger.info("Using whisper-server (Vulkan iGPU large-v3-turbo) for live transcription")
        else:
            from services.transcription_service import transcription_service
            if not transcription_service.is_ready:
                logger.error("No transcription backend available - live transcription disabled")
                return
            logger.info(f"Whisper-server unavailable, falling back to CPU: model={transcription_service.current_model_id}")

        # Speaker diarization (lightweight, numpy-based)
        from services.speaker_diarization_onnx import diarize_audio, assign_speakers_to_transcript
        use_diarization = True
        logger.info("Speaker diarization enabled (acoustic feature clustering)")

        # Wait for file to be created (up to 10 seconds)
        file_wait_count = 0
        while not os.path.exists(self.current_file) and file_wait_count < 10:
            logger.info(f"Waiting for recording file to be created... ({file_wait_count+1}/10)")
            await asyncio.sleep(1)
            file_wait_count += 1

        if not os.path.exists(self.current_file):
            logger.error(f"Recording file never created: {self.current_file}")
            return

        logger.info(f"Recording file found, starting live transcription monitoring")

        while self.is_active:
            try:
                if not os.path.exists(self.current_file):
                    logger.error(f"Recording file disappeared: {self.current_file}")
                    break

                file_size = os.path.getsize(self.current_file)
                logger.debug(f"File size: {file_size} bytes, last position: {self.last_position}")

                # WAV header is 44 bytes, each sample is 2 bytes (16-bit)
                available_samples = (file_size - 44) // 2

                if available_samples - self.last_position >= chunk_samples:
                    logger.info(f"Processing chunk: {available_samples} samples available, need {chunk_samples}")
                    try:
                        with sf.SoundFile(self.current_file, 'r') as audio_file:
                            start_position = max(0, self.last_position - overlap_samples)
                            audio_file.seek(start_position)
                            read_samples = chunk_samples + (self.last_position - start_position)
                            chunk_data = audio_file.read(read_samples)

                        if len(chunk_data) > 0:
                            chunk_duration_actual = len(chunk_data) / recording_sample_rate
                            if chunk_duration_actual < min_chunk_duration:
                                logger.debug(f"Skipping short chunk: {chunk_duration_actual:.1f}s")
                                self.last_position += len(chunk_data)
                                continue

                            logger.info(f"Processing chunk: {chunk_duration_actual:.1f}s of audio from position {self.last_position}")

                            # Skip silent chunks to avoid Whisper hallucinations
                            if not _chunk_has_speech(chunk_data):
                                self.consecutive_silent_chunks += 1
                                logger.info(f"Skipping silent chunk at position {self.last_position} "
                                            f"(silence: {self.consecutive_silent_chunks}/{self.max_silent_chunks})")
                                self.last_position += chunk_samples

                                # Check if silence threshold exceeded for auto-stop
                                if self.consecutive_silent_chunks >= self.max_silent_chunks:
                                    await self._trigger_auto_stop()
                                continue

                            # Speech detected - reset silence counter
                            if self.consecutive_silent_chunks > 0:
                                logger.info(f"Speech resumed after {self.consecutive_silent_chunks} silent chunks")
                            self.consecutive_silent_chunks = 0

                            # Normalize audio for Whisper (USB mics often output very low levels)
                            peak = np.max(np.abs(chunk_data))
                            if 0 < peak < 0.1:
                                # Audio is very quiet, normalize to ~0.5 peak
                                gain = 0.5 / peak
                                chunk_data_normalized = chunk_data * gain
                                logger.info(f"Applied {gain:.1f}x gain (peak was {peak:.4f})")
                            else:
                                chunk_data_normalized = chunk_data

                            # Save chunk to temporary WAV file
                            tmp_fd, tmp_path = tempfile.mkstemp(suffix='.wav')
                            os.close(tmp_fd)

                            if use_whisper_server:
                                # Whisper-server handles resampling via ffmpeg (--convert flag)
                                sf.write(tmp_path, chunk_data_normalized, recording_sample_rate)
                                try:
                                    result = await asyncio.get_event_loop().run_in_executor(
                                        None,
                                        whisper_server_client.transcribe_file,
                                        tmp_path
                                    )
                                finally:
                                    os.unlink(tmp_path)
                            else:
                                # CPU fallback: resample to 16kHz first
                                import librosa
                                chunk_resampled = librosa.resample(
                                    chunk_data_normalized,
                                    orig_sr=recording_sample_rate,
                                    target_sr=target_sample_rate
                                )
                                sf.write(tmp_path, chunk_resampled, target_sample_rate)
                                try:
                                    from services.transcription_service import transcription_service as ts_fallback
                                    result = await asyncio.get_event_loop().run_in_executor(
                                        None,
                                        ts_fallback.transcribe_file,
                                        tmp_path
                                    )
                                finally:
                                    os.unlink(tmp_path)

                            # Run speaker diarization on the chunk (in parallel with processing)
                            diar_segments = []
                            if use_diarization and result and (result.get("segments") or result.get("text")):
                                try:
                                    import librosa
                                    chunk_16k = librosa.resample(
                                        chunk_data,
                                        orig_sr=recording_sample_rate,
                                        target_sr=target_sample_rate
                                    )
                                    diar_segments = await asyncio.get_event_loop().run_in_executor(
                                        None, diarize_audio, chunk_16k, target_sample_rate
                                    )
                                    if diar_segments:
                                        speaker_ids = set(s.speaker for s in diar_segments)
                                        logger.info(f"Diarization: {len(speaker_ids)} speaker(s) in chunk")
                                except Exception as e:
                                    logger.debug(f"Diarization skipped: {e}")

                            if result and result.get("segments"):
                                # Assign speakers to transcript segments
                                enriched = result["segments"]
                                if diar_segments:
                                    enriched = assign_speakers_to_transcript(enriched, diar_segments)

                                for segment in enriched:
                                    text = segment.get("text", "").strip()
                                    if not text or len(text) < 3:
                                        continue

                                    # Filter known Whisper hallucinations
                                    if _is_hallucination(text):
                                        logger.info(f"Filtered hallucination: '{text}'")
                                        continue

                                    # Adjust timestamps to absolute position in recording
                                    seg_start = segment.get("start", 0)
                                    seg_end = segment.get("end", 0)

                                    # Convert NumPy types to Python float
                                    seg_start = float(seg_start.item()) if hasattr(seg_start, 'item') else float(seg_start)
                                    seg_end = float(seg_end.item()) if hasattr(seg_end, 'item') else float(seg_end)

                                    seg_start += self.last_position / recording_sample_rate
                                    seg_end += self.last_position / recording_sample_rate

                                    conf_val = segment.get("confidence", 0.95)
                                    conf_val = float(conf_val.item()) if hasattr(conf_val, 'item') else float(conf_val)

                                    broadcast_segment = {
                                        "text": text,
                                        "start": seg_start,
                                        "end": seg_end,
                                        "speaker": segment.get("speaker"),
                                        "confidence": conf_val
                                    }

                                    logger.info(f"Broadcasting segment: '{text[:50]}...'")

                                    from api.websocket_auto_summary import broadcast_transcription_segment
                                    await broadcast_transcription_segment(
                                        self.session_id,
                                        broadcast_segment
                                    )

                                    self.transcript_buffer.append(broadcast_segment)

                                    from api.websocket_auto_summary import session_word_counts
                                    current_word_count = session_word_counts.get(self.session_id, 0)

                                    await self._check_and_trigger_summary(
                                        self.session_id,
                                        current_word_count
                                    )

                                    await self._publish_to_redis(broadcast_segment)

                                logger.info(f"Transcribed chunk: {len(result['segments'])} segments")
                            elif result and result.get("text"):
                                # Result has text but no segments - broadcast as single segment
                                text = result["text"].strip()
                                if text and len(text) >= 3 and not _is_hallucination(text):
                                    broadcast_segment = {
                                        "text": text,
                                        "start": float(self.last_position / recording_sample_rate),
                                        "end": float((self.last_position + chunk_samples) / recording_sample_rate),
                                        "speaker": None,
                                        "confidence": result.get("confidence", 0.95)
                                    }

                                    logger.info(f"Broadcasting text: '{text[:50]}...'")

                                    from api.websocket_auto_summary import broadcast_transcription_segment
                                    await broadcast_transcription_segment(
                                        self.session_id,
                                        broadcast_segment
                                    )

                                    self.transcript_buffer.append(broadcast_segment)

                                    from api.websocket_auto_summary import session_word_counts
                                    current_word_count = session_word_counts.get(self.session_id, 0)
                                    await self._check_and_trigger_summary(
                                        self.session_id, current_word_count
                                    )
                                    await self._publish_to_redis(broadcast_segment)

                                logger.info("Transcribed chunk: 1 segment (from text)")
                            else:
                                logger.warning("No segments returned from transcription")

                            self.last_position += chunk_samples

                    except Exception as e:
                        logger.error(f"Error reading/transcribing audio chunk: {e}", exc_info=True)
                else:
                    needed_samples = chunk_samples - (available_samples - self.last_position)
                    needed_seconds = needed_samples / recording_sample_rate
                    logger.debug(f"Waiting for more audio: need {needed_seconds:.1f} more seconds")

                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Live transcription error: {e}")
                await asyncio.sleep(2)
    
    async def _trigger_auto_stop(self):
        """Trigger auto-stop due to prolonged silence.
        Sets is_active=False to exit the monitoring loop, publishes a Redis
        event so the recording API can finalize the session, and broadcasts
        a WebSocket message so the frontend can update its UI.
        """
        if self.auto_stop_triggered:
            return  # Prevent duplicate triggers

        self.auto_stop_triggered = True
        silence_duration = self.consecutive_silent_chunks * 15  # seconds
        logger.warning(
            f"Auto-stopping recording due to prolonged silence "
            f"({silence_duration}s / {self.consecutive_silent_chunks} chunks) "
            f"for session {self.session_id}"
        )

        # Stop the monitoring loop
        self.is_active = False

        # Publish auto-stop event to Redis so the recording API can finalize
        try:
            if not self.redis_client:
                self.redis_client = await redis.from_url(REDIS_URL, decode_responses=True)

            channel = f"recording:{self.session_id}:auto-stop"
            message = json.dumps({
                "session_id": self.session_id,
                "reason": "prolonged_silence",
                "silent_chunks": self.consecutive_silent_chunks,
                "silence_duration_seconds": silence_duration,
                "timestamp": time.time(),
            })
            await self.redis_client.publish(channel, message)
            logger.info(f"Published auto-stop event to Redis channel: {channel}")
        except Exception as e:
            logger.error(f"Failed to publish auto-stop event to Redis: {e}")

        # Broadcast auto-stop notification via WebSocket
        try:
            from api.websocket_auto_summary import broadcast_transcription_segment
            await broadcast_transcription_segment(
                self.session_id,
                {
                    "text": "",
                    "type": "auto_stop",
                    "reason": "prolonged_silence",
                    "silence_duration_seconds": silence_duration,
                    "start": float(self.last_position / 44100),
                    "end": float(self.last_position / 44100),
                    "speaker": None,
                    "confidence": 1.0,
                }
            )
            logger.info(f"Broadcast auto-stop WebSocket notification for session {self.session_id}")
        except Exception as e:
            logger.error(f"Failed to broadcast auto-stop via WebSocket: {e}")

    async def _check_and_trigger_summary(self, session_id: str, total_words: int):
        """Check if we should trigger a progressive summary"""
        # Fixed 500-word intervals for progressive summaries
        interval = 500
        
        if session_id not in self.triggered_intervals:
            self.triggered_intervals[session_id] = 0  # Track last trigger point
        
        # Check if we've passed the next 500-word threshold
        next_threshold = ((self.triggered_intervals[session_id] // interval) + 1) * interval
        
        if total_words >= next_threshold:
            logger.info(f"🎯 Progressive summary triggered at {total_words} words for session {session_id}")
            
            # Update last trigger point
            self.triggered_intervals[session_id] = total_words
            
            # Trigger progressive summary via progressive_interval_manager
            try:
                from services.progressive_interval_manager import progressive_interval_manager
                await progressive_interval_manager.trigger_progressive_summary(
                    session_id=session_id,
                    current_word_count=total_words,
                    force=True
                )
                logger.info(f"✅ Progressive summary requested for {session_id} at {total_words} words (next at {total_words + interval})")
            except Exception as e:
                logger.error(f"Failed to trigger progressive summary: {e}")
    
    async def _publish_to_redis(self, segment: Dict):
        """Publish transcript segment to Redis for notes generation"""
        try:
            if not self.redis_client:
                self.redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
            
            # Publish to channel for meeting notes service
            channel = f"transcription:{self.session_id}"
            message = json.dumps({
                'text': segment['text'],
                'speaker': segment.get('speaker'),
                'timestamp': time.time(),
                'start': segment.get('start'),
                'end': segment.get('end'),
                'word_count': len(segment['text'].split()),
                'total_words': self.session_word_counts.get(self.session_id, 0)
            })
            
            await self.redis_client.publish(channel, message)
            logger.debug(f"Published segment to Redis channel: {channel}")
            
        except Exception as e:
            logger.error(f"Error publishing to Redis: {e}")
                
# Global instance
live_recording_transcription = LiveRecordingTranscriptionService()