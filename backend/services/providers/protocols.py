from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    async def chat(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 500, temperature: float = 0.7) -> str: ...
    def chat_sync(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 500, temperature: float = 0.7) -> str: ...
    async def chat_stream(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 500, temperature: float = 0.7): ...

    async def health(self) -> dict: ...
    """Return {"available": bool, "endpoint": str, "model": str, "error": str | None}.
    Implementations should hit the upstream's /v1/models or /health and not raise."""


@runtime_checkable
class EmbeddingsProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
    def embed_sync(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class STTProvider(Protocol):
    async def transcribe(self, audio_path: str, *, language: str | None = None) -> str: ...


@runtime_checkable
class TTSProvider(Protocol):
    async def synthesize(self, text: str, *, voice: str | None = None, format: str = "mp3") -> bytes: ...

    async def synthesize_podcast(
        self,
        script: list[dict],
        voices: dict[str, str],
        *,
        format: str = "mp3",
    ) -> bytes: ...
    """Multi-speaker synthesis. Providers without native multi-voice support
    should raise NotImplementedError so the API can return 501."""

    async def list_voices(self) -> list[dict]: ...
    """Return [{"voice_id": str, "label": str}, ...] for the active provider."""

    async def health(self) -> dict: ...


@runtime_checkable
class RerankingProvider(Protocol):
    async def rerank(self, query: str, documents: list[str], top_n: int = 5) -> list[dict]: ...


@runtime_checkable
class DiarizationProvider(Protocol):
    async def diarize(self, audio_path: str) -> list[dict]: ...

    async def embed(self, audio_path: str) -> dict: ...
    """Return {"embedding": list[float], "embedding_dim": int, "duration_seconds": float, "model": str}."""

    async def identify(self, embedding: list[float], candidates: list[dict], threshold: float = 0.55) -> dict: ...
    """candidates: [{"speaker_id": int, "embedding": list[float]}, ...]
    Returns {"matches": [{"speaker_id": int, "similarity": float, "matched": bool}, ...],
             "best_match": {...} | None}."""
