"""Async TTS rendering pipeline.

Mirrors the upload pipeline pattern: FastAPI handlers enqueue a job, a
background worker pool resolves the org's TTS provider via ProviderRegistry,
runs synthesis, persists the audio, and broadcasts stage changes over a
WebSocket so the frontend can show live progress without polling.

Stages:
    queued -> rendering -> done
                       \\-> failed

For podcasts the rendering stage internally does:
    script_gen (LLM) -> tts_synth (provider) -> persist
But we collapse those into a single 'rendering' stage with progress_pct
updates so the UI stays simple.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import WebSocket
from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import RecordingSession, TtsJob

logger = logging.getLogger(__name__)


def _job_payload(job: TtsJob) -> dict[str, Any]:
    return {
        "job_id": str(job.job_id),
        "session_id": job.session_id,
        "kind": job.kind,
        "stage": job.stage,
        "progress_pct": job.progress_pct,
        "format": job.format,
        "voice": job.voice,
        "host_voice": job.host_voice,
        "analyst_voice": job.analyst_voice,
        "error_message": job.error_message,
        "retry_count": job.retry_count or 0,
        "audio_url": (
            f"/api/sessions/{job.session_id}/tts/{job.kind}.{job.format}"
            if job.stage == "done"
            else None
        ),
        "started_at": job.job_started_at.isoformat() if job.job_started_at else None,
        "completed_at": job.job_completed_at.isoformat() if job.job_completed_at else None,
    }


class TtsWebSocketManager:
    """Per-job WebSocket fan-out. Multiple browser tabs can subscribe to the
    same job_id; each receives every stage update."""

    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, job_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.setdefault(job_id, set()).add(ws)

    def disconnect(self, job_id: str, ws: WebSocket) -> None:
        peers = self._connections.get(job_id)
        if peers and ws in peers:
            peers.discard(ws)
            if not peers:
                self._connections.pop(job_id, None)

    async def broadcast(self, job_id: str, payload: dict[str, Any]) -> None:
        peers = list(self._connections.get(job_id, ()))
        for ws in peers:
            try:
                await ws.send_json(payload)
            except Exception:
                self.disconnect(job_id, ws)


ws_manager = TtsWebSocketManager()


class TtsRenderQueue:
    """Asyncio worker pool for TTS rendering."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        concurrency = max(1, int(os.getenv("TTS_RENDER_CONCURRENCY", "2")))
        for _ in range(concurrency):
            self._workers.append(asyncio.create_task(self._worker()))
        logger.info(f"TTS render queue started with {concurrency} workers")

    async def stop(self) -> None:
        self._running = False
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()

    async def enqueue(self, job_id: str) -> None:
        await self._queue.put(job_id)

    async def _worker(self) -> None:
        while self._running:
            try:
                job_id = await self._queue.get()
            except asyncio.CancelledError:
                break
            try:
                await run_tts_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(f"TTS job {job_id} failed in worker")
            finally:
                self._queue.task_done()


tts_queue = TtsRenderQueue()


async def _set_state(
    db: Session,
    job: TtsJob,
    *,
    stage: Optional[str] = None,
    progress_pct: Optional[int] = None,
    output_path: Optional[str] = None,
    error_message: Optional[str] = None,
    commit: bool = True,
) -> None:
    if stage is not None:
        job.stage = stage
    if progress_pct is not None:
        job.progress_pct = progress_pct
    if output_path is not None:
        job.output_path = output_path
    if error_message is not None:
        job.error_message = error_message[:2000]
    job.updated_at = datetime.now(timezone.utc)
    if commit:
        db.commit()
    await ws_manager.broadcast(str(job.job_id), _job_payload(job))


async def run_tts_job(job_id: str) -> None:
    """Pipeline: resolve provider, render audio, persist to disk, mark done."""
    db = SessionLocal()
    try:
        job = db.query(TtsJob).filter(TtsJob.job_id == uuid.UUID(job_id)).first()
        if not job or job.stage in ("cancelled", "done"):
            return

        job.job_started_at = datetime.now(timezone.utc)
        await _set_state(db, job, stage="rendering", progress_pct=2)

        session = (
            db.query(RecordingSession)
            .filter(
                RecordingSession.id == job.session_id,
                RecordingSession.organization_id == job.organization_id,
            )
            .first()
        )
        if not session:
            raise RuntimeError(f"session {job.session_id} not found in org {job.organization_id}")

        from services.providers import get_provider_registry
        from api.tts import (
            _summary_text,
            _output_path,
            _output_dir,
            _build_podcast_script,
            _normalize_speaker_ids,
        )

        text = _summary_text(session)
        if not text:
            raise RuntimeError("session has no summary or transcript text to synthesize")

        registry = get_provider_registry(db)
        provider = registry.get_tts(job.organization_id)
        provider_name = getattr(provider, "name", "unknown")
        org_slug = session.organization.slug if session.organization else "default"

        if job.kind == "summary":
            await _set_state(db, job, progress_pct=20)
            audio_bytes = await provider.synthesize(text, voice=job.voice, format=job.format)
            if not audio_bytes:
                raise RuntimeError(f"provider {provider_name} returned empty audio")
            await _set_state(db, job, progress_pct=85)
            out_path = _output_path(org_slug, job.session_id, "summary", job.format)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(audio_bytes)
            await _set_state(db, job, progress_pct=100, output_path=str(out_path), commit=False)

        elif job.kind == "podcast":
            if not getattr(provider, "supports_podcast", False):
                raise RuntimeError(
                    f"provider {provider_name} does not support multi-voice podcasts"
                )
            await _set_state(db, job, progress_pct=15)
            script = await _build_podcast_script(registry, job.organization_id, text)
            if not script:
                raise RuntimeError("could not generate a podcast script")
            await _set_state(db, job, progress_pct=40)
            speakers = _normalize_speaker_ids(
                script,
                host_voice=job.host_voice or "alice",
                analyst_voice=job.analyst_voice or "frank",
            )
            audio_bytes = await provider.synthesize_podcast(script, speakers, format=job.format)
            if not audio_bytes:
                raise RuntimeError(f"provider {provider_name} returned empty audio")
            await _set_state(db, job, progress_pct=90)
            out_path = _output_path(org_slug, job.session_id, "podcast", job.format)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(audio_bytes)
            script_path = _output_dir(org_slug, job.session_id) / "podcast_script.json"
            script_path.write_text(json.dumps(script, ensure_ascii=False, indent=2))
            await _set_state(db, job, progress_pct=100, output_path=str(out_path), commit=False)

        else:
            raise RuntimeError(f"unknown TTS job kind: {job.kind}")

        job.stage = "done"
        job.job_completed_at = datetime.now(timezone.utc)
        job.updated_at = datetime.now(timezone.utc)
        db.commit()
        await ws_manager.broadcast(str(job.job_id), _job_payload(job))
        logger.info(f"TTS job {job_id} ({job.kind}) completed for session {job.session_id}")

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(f"TTS job {job_id} failed: {exc}")
        db.rollback()
        job = db.query(TtsJob).filter(TtsJob.job_id == uuid.UUID(job_id)).first()
        if job:
            job.stage = "failed"
            job.error_message = str(exc)[:2000]
            job.job_completed_at = datetime.now(timezone.utc)
            job.updated_at = datetime.now(timezone.utc)
            db.commit()
            await ws_manager.broadcast(str(job.job_id), _job_payload(job))
    finally:
        db.close()
