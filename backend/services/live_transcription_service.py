"""
Live Transcription Service - Time-Shift Recording
Continuously transcribes audio with circular buffer and time-rewind capability
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable
from collections import deque
import numpy as np

from services.npu_whisper_transcriber import NPUWhisperTranscriber
from audio_level_direct_usb import DirectUSBAudioMonitor

logger = logging.getLogger(__name__)

class TranscriptionSegment:
    def __init__(self, text: str, timestamp: datetime, confidence: float = 0.95, speaker: str = "Speaker"):
        self.text = text
        self.timestamp = timestamp
        self.confidence = confidence
        self.speaker = speaker
        self.audio_data: Optional[bytes] = None  # Store audio for retroactive saving

class CircularTranscriptionBuffer:
    """Circular buffer for storing transcription segments with audio data"""
    
    def __init__(self, max_duration_minutes: int = 60):
        self.max_duration = timedelta(minutes=max_duration_minutes)
        self.segments: deque[TranscriptionSegment] = deque()
        self.audio_chunks: deque[tuple] = deque()  # (timestamp, audio_data)
        self.total_size_bytes = 0
        self.max_size_bytes = 96 * 1024 * 1024 * 1024  # 96GB limit
        
    def add_segment(self, segment: TranscriptionSegment, audio_data: bytes = None):
        """Add a new transcription segment to the circular buffer"""
        if audio_data:
            segment.audio_data = audio_data
            self.audio_chunks.append((segment.timestamp, audio_data))
            self.total_size_bytes += len(audio_data)
        
        self.segments.append(segment)
        self._cleanup_old_data()
        
    def _cleanup_old_data(self):
        """Remove old segments and audio to stay within memory limits"""
        cutoff_time = datetime.now() - self.max_duration
        
        # Remove old segments
        while self.segments and self.segments[0].timestamp < cutoff_time:
            old_segment = self.segments.popleft()
            if old_segment.audio_data:
                self.total_size_bytes -= len(old_segment.audio_data)
                
        # Remove old audio chunks
        while self.audio_chunks and self.audio_chunks[0][0] < cutoff_time:
            old_timestamp, old_audio = self.audio_chunks.popleft()
            
        # Emergency cleanup if we exceed memory limit
        while self.total_size_bytes > self.max_size_bytes and self.audio_chunks:
            old_timestamp, old_audio = self.audio_chunks.popleft()
            self.total_size_bytes -= len(old_audio)
            logger.warning(f"Emergency cleanup: removed audio chunk, total size now: {self.total_size_bytes / (1024*1024*1024):.1f}GB")
    
    def get_segments_since(self, since_minutes: int) -> List[TranscriptionSegment]:
        """Get all segments from the last N minutes"""
        cutoff_time = datetime.now() - timedelta(minutes=since_minutes)
        return [seg for seg in self.segments if seg.timestamp >= cutoff_time]
        
    def get_audio_since(self, since_minutes: int) -> List[bytes]:
        """Get all audio chunks from the last N minutes"""
        cutoff_time = datetime.now() - timedelta(minutes=since_minutes)
        return [audio for timestamp, audio in self.audio_chunks if timestamp >= cutoff_time]

class LiveTranscriptionService:
    """Main service for continuous transcription with time-shift functionality"""
    
    def __init__(self):
        self.transcriber = None
        self.audio_monitor = None
        self.buffer = CircularTranscriptionBuffer(max_duration_minutes=60)
        
        # Configuration
        self.is_enabled = False
        self.display_enabled = True
        self.buffer_enabled = True
        self.buffer_duration_minutes = 60
        
        # Active connections
        self.websocket_connections: List = []
        self.callback_functions: List[Callable] = []
        
        # Statistics
        self.segments_processed = 0
        self.total_audio_processed_mb = 0
        
    async def start(self):
        """Start the live transcription service"""
        if self.is_enabled:
            logger.warning("Live transcription already running")
            return
            
        try:
            # Initialize NPU transcriber
            self.transcriber = NPUWhisperTranscriber()
            await asyncio.to_thread(self.transcriber.initialize)
            
            # Initialize audio monitor
            self.audio_monitor = DirectUSBAudioMonitor()
            
            self.is_enabled = True
            logger.info("🔴 Live Transcription Service STARTED")
            logger.info(f"📊 Buffer: {self.buffer_duration_minutes} minutes, Display: {self.display_enabled}")
            
            # Start the main processing loop
            asyncio.create_task(self._processing_loop())
            
        except Exception as e:
            logger.error(f"Failed to start live transcription: {e}")
            raise
    
    async def stop(self):
        """Stop the live transcription service"""
        self.is_enabled = False
        if self.audio_monitor:
            await self.audio_monitor.stop()
        logger.info("⏹️ Live Transcription Service STOPPED")
    
    async def _processing_loop(self):
        """Main processing loop - the heart of the time-shift system"""
        audio_buffer = bytearray()
        chunk_size = 44100 * 2  # 2 seconds of audio at 44.1kHz
        
        logger.info("🚀 Starting continuous transcription loop with NPU")
        
        while self.is_enabled:
            try:
                # Get audio chunk from USB mic
                if self.audio_monitor:
                    audio_chunk = await self.audio_monitor.get_audio_chunk(duration_ms=2000)
                    if audio_chunk:
                        audio_buffer.extend(audio_chunk)
                        self.total_audio_processed_mb += len(audio_chunk) / (1024 * 1024)
                
                # Process when we have enough audio
                if len(audio_buffer) >= chunk_size:
                    chunk_to_process = bytes(audio_buffer[:chunk_size])
                    audio_buffer = audio_buffer[chunk_size//2:]  # 50% overlap
                    
                    # Transcribe with NPU
                    if self.transcriber:
                        start_time = time.time()
                        result = await asyncio.to_thread(
                            self.transcriber.transcribe_audio, 
                            chunk_to_process
                        )
                        
                        processing_time = time.time() - start_time
                        
                        if result and result.get('text', '').strip():
                            # Create segment
                            segment = TranscriptionSegment(
                                text=result['text'].strip(),
                                timestamp=datetime.now(),
                                confidence=result.get('confidence', 0.95),
                                speaker=result.get('speaker', 'Speaker')
                            )
                            
                            # Add to buffer if buffering enabled
                            if self.buffer_enabled:
                                self.buffer.add_segment(segment, chunk_to_process)
                            
                            # Send to display if enabled
                            if self.display_enabled:
                                await self._broadcast_segment(segment, processing_time)
                            
                            self.segments_processed += 1
                            
                            if self.segments_processed % 10 == 0:
                                logger.info(f"📊 Processed {self.segments_processed} segments, "
                                          f"Buffer: {len(self.buffer.segments)} segments, "
                                          f"RAM: {self.buffer.total_size_bytes / (1024*1024*1024):.1f}GB")
                
                # Small delay to prevent CPU overload
                await asyncio.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Error in processing loop: {e}")
                await asyncio.sleep(1)  # Back off on error
    
    async def _broadcast_segment(self, segment: TranscriptionSegment, processing_time: float):
        """Broadcast transcription segment to all connected clients"""
        message = {
            "type": "transcription",
            "data": {
                "text": segment.text,
                "timestamp": segment.timestamp.isoformat(),
                "speaker": segment.speaker,
                "confidence": segment.confidence,
                "processing_time_ms": round(processing_time * 1000, 1),
                "npu_active": True
            }
        }
        
        # Send to websocket connections
        for ws in self.websocket_connections[:]:  # Copy list to avoid issues
            try:
                await ws.send_json(message)
            except:
                self.websocket_connections.remove(ws)
        
        # Send to callback functions
        for callback in self.callback_functions:
            try:
                await callback(segment)
            except Exception as e:
                logger.error(f"Callback error: {e}")
    
    def add_websocket(self, websocket):
        """Add a websocket connection for live updates"""
        self.websocket_connections.append(websocket)
        logger.info(f"WebSocket added, {len(self.websocket_connections)} active connections")
    
    def remove_websocket(self, websocket):
        """Remove a websocket connection"""
        if websocket in self.websocket_connections:
            self.websocket_connections.remove(websocket)
        logger.info(f"WebSocket removed, {len(self.websocket_connections)} active connections")
    
    async def create_retroactive_session(self, since_minutes: int, title: str = None) -> Dict:
        """Create a recording session from buffered data (TIME-SHIFT MAGIC!)"""
        if not self.buffer_enabled:
            raise ValueError("Buffer is disabled, cannot create retroactive session")
            
        segments = self.buffer.get_segments_since(since_minutes)
        audio_chunks = self.buffer.get_audio_since(since_minutes)
        
        if not segments:
            raise ValueError(f"No segments found in last {since_minutes} minutes")
        
        session_data = {
            "session_id": f"retro_{int(time.time())}",
            "title": title or f"Time-Shift Recording - Last {since_minutes} minutes",
            "created_at": datetime.now().isoformat(),
            "segments": [
                {
                    "text": seg.text,
                    "timestamp": seg.timestamp.isoformat(),
                    "speaker": seg.speaker,
                    "confidence": seg.confidence
                } for seg in segments
            ],
            "audio_chunks": len(audio_chunks),
            "total_duration_minutes": since_minutes,
            "retroactive": True
        }
        
        logger.info(f"🎬 Created retroactive session: {len(segments)} segments from last {since_minutes} minutes")
        return session_data
    
    def get_status(self) -> Dict:
        """Get current service status"""
        return {
            "enabled": self.is_enabled,
            "display_enabled": self.display_enabled,
            "buffer_enabled": self.buffer_enabled,
            "buffer_duration_minutes": self.buffer_duration_minutes,
            "segments_in_buffer": len(self.buffer.segments),
            "buffer_size_gb": round(self.buffer.total_size_bytes / (1024*1024*1024), 2),
            "segments_processed": self.segments_processed,
            "total_audio_processed_mb": round(self.total_audio_processed_mb, 1),
            "active_connections": len(self.websocket_connections),
            "npu_available": self.transcriber is not None
        }
    
    def configure(self, **kwargs):
        """Configure service settings"""
        if "display_enabled" in kwargs:
            self.display_enabled = kwargs["display_enabled"]
        if "buffer_enabled" in kwargs:
            self.buffer_enabled = kwargs["buffer_enabled"]  
        if "buffer_duration_minutes" in kwargs:
            new_duration = kwargs["buffer_duration_minutes"]
            self.buffer_duration_minutes = new_duration
            self.buffer.max_duration = timedelta(minutes=new_duration)
            
        logger.info(f"📝 Configuration updated: {kwargs}")

# Global service instance
live_transcription_service = LiveTranscriptionService()