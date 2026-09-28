"""TTS provider implementations.

Two providers are supported:

* `KokoroProvider` — small, fast, OpenAI-compatible TTS for short snippets.
  Default for new orgs; lives at `unicorn-kokoro:8880` on bigboy.
* `VibeVoiceProvider` — long-form, multi-speaker TTS for podcast-style outputs.
  Lives at `meet-vibevoice` on midboy1 P40 #1, reached over the LAN at
  `http://<infinity-host>:8882` (no shared docker network with bigboy).

Both implement the same `synthesize()` shape. `synthesize_podcast()` is a
VibeVoice-only feature; KokoroProvider declines with `NotImplementedError`
so callers can fall back gracefully.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class KokoroProvider:
    """OpenAI-compatible TTS via Kokoro-FastAPI on bigboy."""

    name = "kokoro"
    supports_podcast = False

    def __init__(
        self,
        endpoint: str = "http://unicorn-kokoro:8880",
        voice: str = "af_bella",
        api_key: str = "",
    ):
        self.endpoint = (endpoint or "http://unicorn-kokoro:8880").rstrip("/")
        self.voice = voice or "af_bella"
        self.api_key = api_key or ""

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        format: str = "mp3",
    ) -> bytes:
        import httpx

        payload = {
            "input": text,
            "voice": voice or self.voice,
            "response_format": format,
            "model": "kokoro",
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(
                    f"{self.endpoint}/v1/audio/speech",
                    headers=headers,
                    json=payload,
                )
                if response.status_code == 200:
                    return response.content
                logger.error(
                    "Kokoro TTS error %s: %s",
                    response.status_code,
                    response.text[:200],
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Kokoro TTS failed: %s", exc)
        return b""

    async def synthesize_podcast(
        self,
        script: list[dict],
        voices: dict[str, str],
        *,
        format: str = "mp3",
    ) -> bytes:
        # Kokoro is single-voice; the API endpoint should return 501 instead
        # of pretending to support multi-voice.
        raise NotImplementedError("Kokoro TTS does not support multi-speaker podcasts")

    async def list_voices(self) -> list[dict]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.endpoint}/v1/audio/voices")
                if response.status_code == 200:
                    payload = response.json()
                    voices = payload.get("voices", [])
                    return [{"voice_id": v, "label": v} for v in voices]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kokoro voices listing failed: %s", exc)
        return [{"voice_id": "af_bella", "label": "af_bella"}]

    async def health(self) -> dict:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.endpoint}/health")
                if response.status_code == 200:
                    return {"ok": True, "provider": self.name}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "provider": self.name, "error": str(exc)}
        return {"ok": False, "provider": self.name}


class VibeVoiceProvider:
    """Microsoft VibeVoice — long-form, multi-speaker TTS via meet-vibevoice on midboy1."""

    name = "vibevoice"
    supports_podcast = True

    DEFAULT_VOICE = "alice"

    def __init__(
        self,
        endpoint: str = "",
        voice: str = "alice",
        api_key: str = "",
    ):
        self.endpoint = (
            endpoint
            or os.getenv("VIBEVOICE_ENDPOINT")
            or "http://<infinity-host>:8882"
        ).rstrip("/")
        self.voice = voice or self.DEFAULT_VOICE
        self.api_key = api_key or ""

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def synthesize(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        format: str = "mp3",
    ) -> bytes:
        import httpx

        payload = {
            "text": text,
            "voice_id": voice or self.voice,
            "format": format,
        }
        try:
            async with httpx.AsyncClient(timeout=600) as client:
                response = await client.post(
                    f"{self.endpoint}/tts",
                    headers=self._headers(),
                    json=payload,
                )
                if response.status_code == 200:
                    return response.content
                logger.error(
                    "VibeVoice TTS error %s: %s",
                    response.status_code,
                    response.text[:200],
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("VibeVoice TTS failed: %s", exc)
        return b""

    async def synthesize_podcast(
        self,
        script: list[dict],
        voices: dict[str, str],
        *,
        format: str = "mp3",
    ) -> bytes:
        """Render a multi-speaker conversation.

        `script` is `[{speaker_id, text}, ...]`; `voices` maps each speaker_id to
        a voice preset. Long meetings can take several minutes — caller should
        run this in a background task and stream/poll status.
        """
        import httpx

        payload = {
            "script": script,
            "voices": voices or {},
            "format": format,
        }
        try:
            async with httpx.AsyncClient(timeout=900) as client:
                response = await client.post(
                    f"{self.endpoint}/podcast",
                    headers=self._headers(),
                    json=payload,
                )
                if response.status_code == 200:
                    return response.content
                logger.error(
                    "VibeVoice podcast error %s: %s",
                    response.status_code,
                    response.text[:200],
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("VibeVoice podcast failed: %s", exc)
        return b""

    async def list_voices(self) -> list[dict]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.endpoint}/voices", headers=self._headers())
                if response.status_code == 200:
                    payload = response.json()
                    return [
                        {"voice_id": v.get("voice_id"), "label": v.get("voice_id")}
                        for v in payload.get("voices", [])
                    ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("VibeVoice voices listing failed: %s", exc)
        return [{"voice_id": self.DEFAULT_VOICE, "label": self.DEFAULT_VOICE}]

    async def health(self) -> dict:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.endpoint}/health", headers=self._headers())
                if response.status_code == 200:
                    body = response.json()
                    return {
                        "ok": True,
                        "provider": self.name,
                        "model_loaded": bool(body.get("model_loaded")),
                        "gpu_name": body.get("gpu_name"),
                        "voices": body.get("voices_available"),
                    }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "provider": self.name, "error": str(exc)}
        return {"ok": False, "provider": self.name}
