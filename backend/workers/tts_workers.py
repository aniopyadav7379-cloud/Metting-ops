"""Arq worker: TTS render — v3.18.3.

The two sync TTS handlers (`POST /api/sessions/{id}/tts/summary` and
`/tts/podcast`) can run for 60-120 seconds per call when VibeVoice is
generating a multi-voice podcast. They previously awaited the provider
inline; in v3.18.3 they enqueue here and return 202 + job_id. The
already-existing `TtsJob` table + WebSocket progress flow remains the
preferred path for the in-app UX (richer progress events); this worker
is the fallback for clients that want a simple poll loop.

Idempotency: the worker writes to the same `_output_path` the sync
handler used — a re-run with the same args overwrites the file
deterministically. The HTTP handler short-circuits on cache hit before
enqueueing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from arq import Retry

logger = logging.getLogger(__name__)


async def render_tts_summary_job(
    ctx: dict[str, Any],
    org_id: int,
    org_slug: str,
    session_pk: int,
    voice: str | None,
    fmt: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Render the single-voice summary TTS audio off the request path."""
    import os
    from middleware.request_context import bind_request_id
    bind_request_id(request_id or ctx.get("job_id"))

    from database.database import SessionLocal
    from database.models import RecordingSession
    from api.tts import _summary_text, _output_path, _build_spoken_script
    from services.providers.impl_tts import KokoroProvider
    from services.providers.registry import ProviderRegistry

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        if not session:
            return {"status": "failed", "error": "session_missing", "session_pk": session_pk}

        # Vocal summary: a Qwen-generated SPOKEN narration (distinct from the
        # written summary), cached on the session, read aloud by Kokoro — voice
        # AF Heart by default. Always Kokoro, independent of the org's podcast
        # TTS provider.
        fs = session.final_summary if isinstance(session.final_summary, dict) else {}
        text = (fs.get("spoken_script") or "").strip()
        if not text:
            written = _summary_text(session)
            if not written:
                return {"status": "failed", "error": "no_summary_text", "session_pk": session_pk}
            registry = ProviderRegistry(db)
            text = (await _build_spoken_script(registry, org_id, written) or written).strip()
            if text and text != written:
                new_fs = dict(fs)
                new_fs["spoken_script"] = text
                session.final_summary = new_fs
                try:
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()

        out_path = _output_path(org_slug, session_pk, "summary", fmt)
        provider = KokoroProvider(
            endpoint=os.getenv("KOKORO_ENDPOINT") or "http://192.168.10.14:8880"
        )
        provider_name = provider.name
        voice = voice or "af_heart"

        try:
            audio_bytes = await provider.synthesize(text, voice=voice, format=fmt)
        except Exception as exc:  # noqa: BLE001
            logger.exception("render_tts_summary_job: provider error")
            from services.transient_errors import is_transient_error, retry_transient
            if is_transient_error(exc):
                await retry_transient(ctx, exc)
            return {
                "status": "failed",
                "error": f"provider_error: {exc}"[:500],
                "session_pk": session_pk,
            }

        if not audio_bytes:
            return {
                "status": "failed",
                "error": "empty_audio",
                "session_pk": session_pk,
            }

        out_path.write_bytes(audio_bytes)
        return {
            "status": "completed",
            "session_pk": session_pk,
            "provider": provider_name,
            "voice": voice,
            "format": fmt,
            "bytes": out_path.stat().st_size,
            "audio_url": f"/api/sessions/{session_pk}/tts/summary.{fmt}",
        }
    except asyncio.CancelledError:
        raise
    except Retry:
        raise
    except Exception as exc:
        logger.exception("render_tts_summary_job: crashed")
        from services.transient_errors import is_transient_error, retry_transient
        if is_transient_error(exc):
            await retry_transient(ctx, exc)
        return {
            "status": "failed",
            "error": str(exc)[:500],
            "session_pk": session_pk,
        }
    finally:
        db.close()


async def render_tts_podcast_job(
    ctx: dict[str, Any],
    org_id: int,
    org_slug: str,
    session_pk: int,
    host_voice: str | None,
    analyst_voice: str | None,
    fmt: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Render the two-voice podcast TTS audio + script off the request path."""
    import json
    from middleware.request_context import bind_request_id
    bind_request_id(request_id or ctx.get("job_id"))

    from database.database import SessionLocal
    from database.models import RecordingSession
    from api.tts import (
        _summary_text,
        _output_path,
        _output_dir,
        _build_podcast_script,
        _normalize_speaker_ids,
    )
    from services.providers.registry import ProviderRegistry

    db = SessionLocal()
    try:
        session = db.query(RecordingSession).filter(RecordingSession.id == session_pk).first()
        if not session:
            return {"status": "failed", "error": "session_missing", "session_pk": session_pk}

        text = _summary_text(session)
        if not text:
            return {"status": "failed", "error": "no_summary_text", "session_pk": session_pk}

        registry = ProviderRegistry(db)
        provider = registry.get_tts(org_id)
        if not getattr(provider, "supports_podcast", False):
            return {
                "status": "failed",
                "error": "provider_no_podcast_support",
                "provider": getattr(provider, "name", "unknown"),
                "session_pk": session_pk,
            }

        script = await _build_podcast_script(registry, org_id, text)
        if not script:
            return {"status": "failed", "error": "script_generation_failed", "session_pk": session_pk}

        speakers = _normalize_speaker_ids(
            script,
            host_voice=host_voice or "alice",
            analyst_voice=analyst_voice or "frank",
        )

        try:
            audio_bytes = await provider.synthesize_podcast(script, speakers, format=fmt)
        except NotImplementedError:
            return {"status": "failed", "error": "not_implemented", "session_pk": session_pk}
        except Exception as exc:  # noqa: BLE001
            logger.exception("render_tts_podcast_job: provider error")
            from services.transient_errors import is_transient_error, retry_transient
            if is_transient_error(exc):
                await retry_transient(ctx, exc)
            return {
                "status": "failed",
                "error": f"provider_error: {exc}"[:500],
                "session_pk": session_pk,
            }

        if not audio_bytes:
            return {"status": "failed", "error": "empty_audio", "session_pk": session_pk}

        out_path = _output_path(org_slug, session_pk, "podcast", fmt)
        script_path = _output_dir(org_slug, session_pk) / "podcast_script.json"
        out_path.write_bytes(audio_bytes)
        script_path.write_text(json.dumps(script))

        return {
            "status": "completed",
            "session_pk": session_pk,
            "provider": getattr(provider, "name", "unknown"),
            "format": fmt,
            "bytes": out_path.stat().st_size,
            "audio_url": f"/api/sessions/{session_pk}/tts/podcast.{fmt}",
            "speakers": speakers,
        }
    except asyncio.CancelledError:
        raise
    except Retry:
        raise
    except Exception as exc:
        logger.exception("render_tts_podcast_job: crashed")
        from services.transient_errors import is_transient_error, retry_transient
        if is_transient_error(exc):
            await retry_transient(ctx, exc)
        return {
            "status": "failed",
            "error": str(exc)[:500],
            "session_pk": session_pk,
        }
    finally:
        db.close()
