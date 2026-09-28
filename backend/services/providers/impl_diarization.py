"""Diarization + speaker-embedding providers.

`LocalSpeakerSvcProvider` talks to the speaker-svc container running on
bigboy GPU 1. It implements the full DiarizationProvider protocol —
diarize / embed / identify — so the backend can do everything via one
HTTP service instead of bundling SpeechBrain + Pyannote into the FastAPI
process.

The legacy `LocalDiarizationProvider` name is kept as an alias for back-compat
with code paths that haven't migrated yet.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from .protocols import DiarizationProvider  # noqa: F401  (re-exported)

logger = logging.getLogger(__name__)

DEFAULT_SPEAKER_SVC_URL = os.getenv("SPEAKER_SVC_URL", "http://meet-speaker-svc:8889")
DEFAULT_TIMEOUT = float(os.getenv("SPEAKER_SVC_TIMEOUT", "300"))


class LocalSpeakerSvcProvider:
    """HTTP client for the speaker-svc container."""

    def __init__(self, endpoint: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT):
        self.endpoint = (endpoint or DEFAULT_SPEAKER_SVC_URL).rstrip("/")
        self.timeout = timeout

    # ---------- diarize ----------

    async def diarize(
        self,
        audio_path: str,
        *,
        num_speakers: Optional[int] = None,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
        clustering_threshold: Optional[float] = None,
    ) -> list[dict]:
        """Return a list of diarized segments.

        Shape kept compatible with the legacy provider — list[dict] with at
        least start/end/speaker keys. Each segment may also carry an
        `embedding` key (192-d ECAPA float list) so the caller can pipe
        directly into /identify.

        num_speakers / min_speakers / max_speakers are forwarded to
        speaker-svc when set; the upstream pyannote diarizer respects them.
        Leave all three None for full auto-detection (the default).
        """
        # Always send audio as a multipart upload rather than relying on the
        # legacy audio_path form field. When speaker-svc runs on a different
        # host than the backend (e.g. midboy1 vs bigboy), the path on the
        # backend host does not resolve on the speaker-svc host. Streaming
        # the bytes works regardless of host topology and the in-cluster
        # case is fast enough that the extra copy is invisible.
        import os as _os
        if not _os.path.exists(audio_path):
            raise FileNotFoundError(
                f"speaker-svc /diarize audio file is missing: {audio_path}"
            )

        data: dict[str, str] = {"return_embeddings": "true"}
        if num_speakers is not None:
            data["num_speakers"] = str(num_speakers)
        if min_speakers is not None:
            data["min_speakers"] = str(min_speakers)
        if max_speakers is not None:
            data["max_speakers"] = str(max_speakers)
        if clustering_threshold is not None:
            # speaker-svc fixes this value at startup to avoid rebuilding the
            # pyannote graph (and leaking VRAM) per request.  Older saved
            # upload preferences may still contain a threshold; intentionally
            # ignore it instead of sending a request the service rejects.
            logger.info(
                "Ignoring deprecated per-request diarization threshold %s",
                clustering_threshold,
            )

        retry_attempts = max(1, int(os.getenv("SPEAKER_SVC_RETRY_ATTEMPTS", "3")))
        last_error: Exception | None = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(1, retry_attempts + 1):
                try:
                    # Re-open on every attempt: httpx consumes the multipart
                    # stream even when speaker-svc answers 429/503.
                    with open(audio_path, "rb") as f:
                        files = {
                            "audio": (_os.path.basename(audio_path), f, "audio/wav")
                        }
                        resp = await client.post(
                            f"{self.endpoint}/diarize",
                            data=data,
                            files=files,
                        )
                    resp.raise_for_status()
                    payload = resp.json()
                    segments = payload.get("segments", []) or []
                    for seg in segments:
                        seg.setdefault("backend", payload.get("backend", "unknown"))
                    return segments
                except httpx.HTTPStatusError as exc:
                    last_error = exc
                    if (
                        exc.response.status_code in {429, 503}
                        and attempt < retry_attempts
                    ):
                        try:
                            retry_after = float(
                                exc.response.headers.get("Retry-After", "5")
                            )
                        except (TypeError, ValueError):
                            retry_after = 5.0
                        await asyncio.sleep(max(0.25, min(retry_after, 30.0)))
                        continue
                    raise RuntimeError(
                        "speaker-svc diarization failed "
                        f"(HTTP {exc.response.status_code})"
                    ) from exc
                except httpx.RequestError as exc:
                    last_error = exc
                    if attempt < retry_attempts:
                        await asyncio.sleep(min(2**attempt, 10))
                        continue
                    raise RuntimeError(
                        "speaker-svc diarization request failed"
                    ) from exc

        raise RuntimeError("speaker-svc diarization failed") from last_error

    # ---------- embed ----------

    async def embed(self, audio_path: str) -> dict:
        """Return a single ECAPA embedding for an audio clip."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.endpoint}/embed",
                    data={"audio_path": audio_path},
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("speaker-svc /embed HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
        except Exception as exc:  # noqa: BLE001
            logger.error("speaker-svc /embed failed: %s", exc)
        return {"embedding": [], "embedding_dim": 0, "duration_seconds": 0.0, "model": ""}

    async def embed_bytes(self, audio_bytes: bytes, filename: str = "clip.wav") -> dict:
        """Embed raw audio bytes (used by enrollment uploads from the UI)."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                files = {"audio": (filename, audio_bytes, "application/octet-stream")}
                resp = await client.post(f"{self.endpoint}/embed", files=files)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("speaker-svc /embed (bytes) HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
        except Exception as exc:  # noqa: BLE001
            logger.error("speaker-svc /embed (bytes) failed: %s", exc)
        return {"embedding": [], "embedding_dim": 0, "duration_seconds": 0.0, "model": ""}

    # ---------- identify ----------

    async def identify(self, embedding: list[float], candidates: list[dict], threshold: float = 0.55) -> dict:
        """Score a query embedding against enrolled-speaker centroids."""
        if not embedding:
            return {"matches": [], "best_match": None}
        try:
            payload = {
                "embedding": embedding,
                "candidates": candidates,
                "threshold": threshold,
            }
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.endpoint}/identify", json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("speaker-svc /identify HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
        except Exception as exc:  # noqa: BLE001
            logger.error("speaker-svc /identify failed: %s", exc)
        return {"matches": [], "best_match": None}

    # ---------- health ----------

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.endpoint}/health")
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("speaker-svc /health failed: %s", exc)
            return {"status": "unreachable", "error": str(exc)}


DEFAULT_SORTFORMER_URL = os.getenv("SORTFORMER_URL", "http://meet-sortformer-svc:8896")


class SortformerSpeakerSvcProvider:
    """Canonical-hybrid diarization provider (v2.2.0).

    Diarization (speaker segmentation) is done by meet-sortformer-svc via
    its multipart ``/diarize-file-upload`` endpoint, which internally asks
    meet-speaker-svc (wespeaker) for the per-turn voice embeddings. The svc
    returns the same response shape as meet-speaker-svc's ``/diarize``, so
    ``diarize()`` here is a drop-in for ``LocalSpeakerSvcProvider.diarize``.

    Everything else — ``embed`` / ``embed_bytes`` / ``identify`` / enrollment
    — still goes to meet-speaker-svc (wespeaker is canonical for speaker
    identity). We hold an inner ``LocalSpeakerSvcProvider`` and delegate
    those methods so the only thing that changes vs the pyannote default is
    *which model draws the speaker boundaries*.
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        *,
        speaker_svc_endpoint: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.endpoint = (endpoint or DEFAULT_SORTFORMER_URL).rstrip("/")
        self.timeout = timeout
        # wespeaker stays canonical for embed/identify/enrollment.
        self._speaker_svc = LocalSpeakerSvcProvider(
            endpoint=speaker_svc_endpoint, timeout=timeout
        )

    # ---------- diarize (sortformer hybrid) ----------

    async def diarize(
        self,
        audio_path: str,
        *,
        num_speakers: Optional[int] = None,
        min_speakers: Optional[int] = None,
        max_speakers: Optional[int] = None,
        clustering_threshold: Optional[float] = None,
    ) -> list[dict]:
        """Diarize via sortformer-svc /diarize-file-upload.

        Returns the same list[dict] shape as LocalSpeakerSvcProvider:
        each segment carries start / end / speaker and (best-effort)
        embedding. The speaker-count and clustering hints pyannote honours
        are accepted for protocol compatibility but ignored — Sortformer is
        fixed at a 4-speaker architecture and has no clustering threshold.
        """
        import os as _os
        if not _os.path.exists(audio_path):
            logger.error(
                "sortformer-svc /diarize-file-upload: audio missing on disk: %s",
                audio_path,
            )
            return []
        if any(v is not None for v in (num_speakers, min_speakers, max_speakers, clustering_threshold)):
            logger.debug(
                "sortformer hybrid ignores speaker-count/clustering hints "
                "(num=%s min=%s max=%s thr=%s) — model is fixed 4-speaker",
                num_speakers, min_speakers, max_speakers, clustering_threshold,
            )

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                with open(audio_path, "rb") as f:
                    files = {"audio": (_os.path.basename(audio_path), f, "audio/wav")}
                    resp = await client.post(
                        f"{self.endpoint}/diarize-file-upload",
                        data={"return_embeddings": "true"},
                        files=files,
                    )
                resp.raise_for_status()
                payload = resp.json()
                segments = payload.get("segments", []) or []
                for seg in segments:
                    seg.setdefault("backend", payload.get("backend", "sortformer-hybrid"))
                return segments
        except httpx.HTTPStatusError as exc:
            logger.error(
                "sortformer-svc /diarize-file-upload HTTP %s: %s",
                exc.response.status_code, exc.response.text[:200],
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("sortformer-svc /diarize-file-upload failed: %s", exc)
        return []

    # ---------- embed / identify / health delegate to wespeaker ----------

    async def embed(self, audio_path: str) -> dict:
        return await self._speaker_svc.embed(audio_path)

    async def embed_bytes(self, audio_bytes: bytes, filename: str = "clip.wav") -> dict:
        return await self._speaker_svc.embed_bytes(audio_bytes, filename)

    async def identify(self, embedding: list[float], candidates: list[dict], threshold: float = 0.55) -> dict:
        return await self._speaker_svc.identify(embedding, candidates, threshold)

    async def health(self) -> dict:
        """Report the sortformer-svc health plus the wespeaker svc it
        depends on for embeddings, so a degraded embedding path is visible."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.endpoint}/health")
                resp.raise_for_status()
                out = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("sortformer-svc /health failed: %s", exc)
            out = {"status": "unreachable", "error": str(exc)}
        out["speaker_svc"] = await self._speaker_svc.health()
        return out


# Back-compat alias. New code should use LocalSpeakerSvcProvider.
LocalDiarizationProvider = LocalSpeakerSvcProvider
