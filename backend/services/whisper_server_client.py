"""
Whisper Server HTTP Client
Calls whisper.cpp server (Vulkan iGPU) for fast transcription via HTTP API.
Server runs on port 8178 with large-v3-turbo model (~9.6x realtime on 780M).
"""

import os
import logging
import httpx
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

WHISPER_SERVER_URL = os.getenv("WHISPER_SERVER_URL", "http://localhost:8178")


class WhisperServerClient:
    """HTTP client for whisper.cpp server"""

    def __init__(self, base_url: str = WHISPER_SERVER_URL):
        self.base_url = base_url
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        """Check if whisper-server is healthy"""
        try:
            from middleware.request_context import outbound_request_headers
            with httpx.Client(timeout=5.0, headers=outbound_request_headers()) as client:
                resp = client.get(f"{self.base_url}/health")
                self._available = resp.status_code == 200 and resp.json().get("status") == "ok"
                return self._available
        except Exception:
            self._available = False
            return False

    def transcribe_file(self, file_path: str, language: str = "en") -> Optional[Dict[str, Any]]:
        """
        Transcribe an audio file via whisper-server HTTP API.

        Args:
            file_path: Path to audio file (WAV, MP3, etc - server has --convert)
            language: Language code (default: en)

        Returns:
            Dict with 'text', 'segments' (with start/end/text), 'duration'
            or None on failure
        """
        if not os.path.exists(file_path):
            logger.error(f"Audio file not found: {file_path}")
            return None

        try:
            with open(file_path, "rb") as f:
                files = {"file": (os.path.basename(file_path), f, "audio/wav")}
                data = {
                    "response_format": "verbose_json",
                    "language": language,
                    "temperature": "0.0",
                }

                # Timeout scales with file size: base 30s + 1s per MB
                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                timeout = max(30.0, 30.0 + file_size_mb)

                from middleware.request_context import outbound_request_headers
                with httpx.Client(timeout=timeout, headers=outbound_request_headers()) as client:
                    resp = client.post(
                        f"{self.base_url}/inference",
                        files=files,
                        data=data,
                    )

            if resp.status_code != 200:
                logger.error(f"Whisper server returned {resp.status_code}: {resp.text[:200]}")
                return None

            result = resp.json()

            # Normalize verbose_json response to our standard format
            segments = []
            for seg in result.get("segments", []):
                segments.append({
                    "text": seg.get("text", "").strip(),
                    "start": seg.get("start", 0.0),
                    "end": seg.get("end", 0.0),
                    "confidence": 1.0 - seg.get("no_speech_prob", 0.0),
                    "speaker": None,  # whisper.cpp doesn't do diarization
                })

            return {
                "text": result.get("text", "").strip(),
                "segments": segments,
                "duration": result.get("duration", 0.0),
                "language": result.get("language", language),
                "confidence": 0.95,
            }

        except httpx.TimeoutException:
            logger.error(f"Whisper server timeout transcribing {file_path}")
            return None
        except Exception as e:
            logger.error(f"Whisper server client error: {e}")
            return None


# Singleton
whisper_server_client = WhisperServerClient()
