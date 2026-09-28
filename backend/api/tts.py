"""TTS API — render meeting summaries and podcast recaps as audio.

Two flavours:

* `POST /api/sessions/{session_id}/tts/summary` — single-voice rendering of the
  session summary. Works for any TTS provider.
* `POST /api/sessions/{session_id}/tts/podcast` — multi-voice 2-host podcast
  recap. Requires a provider that advertises `supports_podcast = True`. We use
  the org's configured LLM (`task="quality"`) to convert the meeting summary
  into a host/analyst script first, then hand off to TTS.

Audio outputs are persisted under
`/app/recordings/tts/<org_slug>/<session_id>/<type>.<ext>` and re-served by the
matching `GET` endpoints. Subsequent calls return the cached file unless the
caller passes `?regenerate=true`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from auth.tier import gate_feature_for_caller
from auth.ws_auth import enforce_ws_auth
from database.database import get_db
from database.models import RecordingSession
from services.providers.registry import ProviderRegistry, get_provider_registry

logger = logging.getLogger(__name__)
router = APIRouter(tags=["TTS"])
ws_router = APIRouter(tags=["TTS"])

RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "/app/recordings"))
TTS_SUBDIR = "tts"


# ---------- shared helpers ----------


def _slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9-]+", "-", value).strip("-")
    return value or "default"


def _output_dir(org_slug: str, session_id: int) -> Path:
    target = RECORDINGS_DIR / TTS_SUBDIR / _slugify(org_slug) / str(session_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _output_path(org_slug: str, session_id: int, kind: str, fmt: str) -> Path:
    ext = "mp3" if fmt.lower() == "mp3" else "wav"
    return _output_dir(org_slug, session_id) / f"{kind}.{ext}"


def _media_type_for(fmt: str) -> str:
    return "audio/mpeg" if fmt.lower() == "mp3" else "audio/wav"


def _summary_text(session: RecordingSession) -> str:
    """Best-effort plain-text summary for TTS."""
    final = session.final_summary or {}
    legacy = session.summary or {}
    if isinstance(legacy, str):
        legacy = {"executive": legacy}

    pieces: list[str] = []
    exec_text = (
        final.get("executive")
        or legacy.get("executive")
        or (legacy.get("analysis", {}) or {}).get("executive")
        or ""
    )
    if exec_text:
        pieces.append(exec_text.strip())

    bullets = (
        final.get("bullets")
        or legacy.get("bullets")
        or (legacy.get("analysis", {}) or {}).get("bullets")
        or []
    )
    if bullets:
        pieces.append("Key discussion points: " + " ".join(
            f"{i + 1}. {b}." for i, b in enumerate(bullets) if isinstance(b, str)
        ))

    actions = (
        final.get("actions")
        or legacy.get("actions")
        or (legacy.get("analysis", {}) or {}).get("actions")
        or []
    )
    if actions:
        rendered = []
        for a in actions:
            if isinstance(a, dict):
                rendered.append(a.get("task") or a.get("description") or "")
            elif isinstance(a, str):
                rendered.append(a)
        rendered = [r for r in rendered if r]
        if rendered:
            pieces.append("Action items: " + " ".join(f"{i + 1}. {r}." for i, r in enumerate(rendered)))

    decisions = (
        final.get("decisions")
        or legacy.get("decisions")
        or (legacy.get("analysis", {}) or {}).get("decisions")
        or []
    )
    if decisions:
        rendered = [d if isinstance(d, str) else d.get("description", "") for d in decisions]
        rendered = [r for r in rendered if r]
        if rendered:
            pieces.append("Decisions made: " + " ".join(f"{i + 1}. {r}." for i, r in enumerate(rendered)))

    text = "\n\n".join(pieces).strip()
    if text:
        return text
    if session.transcript_simple:
        return session.transcript_simple.strip()
    if session.transcript:
        return session.transcript.strip()
    return ""


def _resolve_session(
    session_id: "int | str",
    db: Session,
    org_id: int,
    user: "User",
    min_level: str = "view",
) -> RecordingSession:
    """Numeric-or-UUID lookup in the active org, with the canonical cross-org
    has_session_access fallback (see session_permissions)."""
    from api.session_permissions import resolve_session_for_user

    session = resolve_session_for_user(db, org_id, session_id, user, min_level)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


# ---------- voices listing ----------


class VoiceItem(BaseModel):
    voice_id: str
    label: str


class VoicesResponse(BaseModel):
    provider: str
    supports_podcast: bool
    voices: list[VoiceItem]


@router.get("/api/tts/voices", response_model=VoicesResponse)
async def list_tts_voices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    registry = ProviderRegistry(db)
    provider = registry.get_tts(active_org.organization.id)
    try:
        voices = await provider.list_voices()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Provider voice listing failed: %s", exc)
        voices = []
    return VoicesResponse(
        provider=getattr(provider, "name", "unknown"),
        supports_podcast=bool(getattr(provider, "supports_podcast", False)),
        voices=[VoiceItem(voice_id=v.get("voice_id"), label=v.get("label", v.get("voice_id"))) for v in voices],
    )


# ---------- summary synthesis ----------


class SummaryTTSResponse(BaseModel):
    session_id: int
    provider: str
    voice: Optional[str] = None
    format: str
    bytes: int
    cached: bool
    audio_url: str


@router.post("/api/sessions/{session_id}/tts/summary")
async def synthesize_summary(
    session_id: str,
    response: Response,
    voice: Optional[str] = Query(None),
    format: str = Query("mp3", pattern="^(mp3|wav)$"),
    regenerate: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Render a single-voice TTS of the session summary.

    v3.18.3 background-jobification: cache-hit still returns 200 with the
    full payload (no work). Cache-miss enqueues an arq job and returns
    202 + `{job_id, status_url}`; the frontend polls `/api/jobs/<id>` for
    completion and then GETs `tts/summary.<ext>` for the bytes.
    """
    gate_feature_for_caller(current_user, "qwen36_summary", active_org)  # v3.29.0 moat fix: vocal summary runs the server LLM + TTS
    session = _resolve_session(session_id, db, active_org.organization.id, current_user, min_level="edit")
    session_id = session.id  # normalize to numeric ID for downstream helpers
    text = _summary_text(session)
    if not text:
        raise HTTPException(status_code=400, detail="Session has no summary or transcript to read aloud.")

    out_path = _output_path(active_org.organization.slug, session_id, "summary", format)
    cached = out_path.exists() and not regenerate

    registry = ProviderRegistry(db)
    provider = registry.get_tts(active_org.organization.id)
    provider_name = getattr(provider, "name", "unknown")

    if cached:
        return SummaryTTSResponse(
            session_id=session_id,
            provider=provider_name,
            voice=voice,
            format=format,
            bytes=out_path.stat().st_size,
            cached=True,
            audio_url=f"/api/sessions/{session_id}/tts/summary.{out_path.suffix.lstrip('.')}",
        )

    # v3.18.3: enqueue + 202. Fall back to inline if arq is unavailable.
    try:
        from services.job_runner import enqueue_job
        job_id = await enqueue_job(
            "render_tts_summary_job",
            active_org.organization.id,
            active_org.organization.slug,
            session_id,
            voice,
            format,
            owner_user_id=current_user.id,
            owner_org_id=active_org.organization.id,
        )
        response.status_code = 202
        return {
            "job_id": job_id,
            "status_url": f"/api/jobs/{job_id}",
            "session_id": session_id,
            "provider": provider_name,
            "voice": voice,
            "format": format,
            "cached": False,
            "audio_url": f"/api/sessions/{session_id}/tts/summary.{out_path.suffix.lstrip('.')}",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("synthesize_summary: arq enqueue failed: %s — running inline", exc)

    try:
        audio_bytes = await provider.synthesize(text, voice=voice, format=format)
    except Exception as exc:  # noqa: BLE001
        logger.exception("TTS synthesize failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"TTS provider error: {exc}")
    if not audio_bytes:
        raise HTTPException(
            status_code=502,
            detail=f"TTS provider '{provider_name}' returned empty audio.",
        )
    out_path.write_bytes(audio_bytes)

    return SummaryTTSResponse(
        session_id=session_id,
        provider=provider_name,
        voice=voice,
        format=format,
        bytes=out_path.stat().st_size,
        cached=False,
        audio_url=f"/api/sessions/{session_id}/tts/summary.{out_path.suffix.lstrip('.')}",
    )


@router.get("/api/sessions/{session_id}/tts/summary.{ext}")
async def get_summary_audio(
    session_id: str,
    ext: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if ext not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="Unsupported format")
    session = _resolve_session(session_id, db, active_org.organization.id, current_user)
    session_id = session.id  # normalize to numeric ID for downstream helpers
    out_path = _output_path(active_org.organization.slug, session_id, "summary", ext)
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Audio not yet generated")
    return FileResponse(
        path=str(out_path),
        media_type=_media_type_for(ext),
        filename=f"summary-{session_id}.{ext}",
    )


# ---------- podcast synthesis ----------


PODCAST_SYSTEM_PROMPT = """You convert a meeting summary into a short, lively two-host podcast recap.

Output rules:
- Output ONLY a JSON object with a single key "script" whose value is an array of turns.
- Each turn is {"speaker_id": "host" or "analyst", "text": "..."} with no extra keys.
- 6 to 12 turns total, alternating speakers, starting with "host".
- Keep each turn to 1-3 sentences. Conversational but informative.
- The "host" introduces the meeting and stitches things together.
- The "analyst" provides context, calls out key decisions, action items, and risks.
- No filler like "[laughs]". No stage directions. Just spoken lines.
- Do NOT mention you are an AI. Do not include preamble or markdown fences.
"""


class PodcastTurn(BaseModel):
    speaker_id: str
    text: str


class PodcastTTSResponse(BaseModel):
    session_id: int
    provider: str
    format: str
    bytes: int
    cached: bool
    audio_url: str
    script: list[PodcastTurn]
    speakers: dict[str, str]


def _coerce_script_payload(raw: str) -> list[dict]:
    """Tolerantly parse the LLM's JSON output. Strips markdown fences, picks
    out the first JSON object, and validates the script shape."""
    if not raw:
        return []
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(json)?", "", cleaned).strip()
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
    # try direct parse
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    if isinstance(payload, list):
        turns = payload
    elif isinstance(payload, dict):
        turns = payload.get("script") or payload.get("turns") or []
    else:
        return []
    out: list[dict] = []
    for t in turns:
        if not isinstance(t, dict):
            continue
        sid = str(t.get("speaker_id") or t.get("speaker") or "host").strip().lower() or "host"
        text = (t.get("text") or "").strip()
        if not text:
            continue
        out.append({"speaker_id": sid, "text": text})
    return out


def _fallback_script(text: str) -> list[dict]:
    """If the LLM is unavailable, produce a simple host/analyst alternation."""
    chunks = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if not chunks:
        return []
    turns = [{"speaker_id": "host", "text": "Welcome back to the Meeting-Ops recap."}]
    speakers = ["analyst", "host"]
    for i, chunk in enumerate(chunks[:10]):
        turns.append({"speaker_id": speakers[i % 2], "text": chunk})
    turns.append({"speaker_id": "host", "text": "That's all for this recap. Thanks for listening."})
    return turns


async def _build_podcast_script(
    registry: ProviderRegistry,
    org_id: int,
    summary_text: str,
) -> list[dict]:
    llm = registry.get_llm(org_id, task="quality")
    user_prompt = (
        "Meeting summary to convert into a podcast recap:\n\n"
        + summary_text.strip()
        + "\n\nReturn the JSON object now."
    )
    try:
        raw = await llm.chat(
            PODCAST_SYSTEM_PROMPT,
            user_prompt,
            max_tokens=900,
            temperature=0.7,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM script generation failed: %s — using fallback", exc)
        raw = ""
    parsed = _coerce_script_payload(raw)
    if not parsed:
        parsed = _fallback_script(summary_text)
    return parsed


SPOKEN_SUMMARY_SYSTEM_PROMPT = (
    "You are a warm, natural-sounding narrator. Rewrite the meeting summary below "
    "as SPOKEN narration for a single voice to read aloud — flowing conversational "
    "prose, NOT bullet points, headings, or markdown. Use short, clear sentences "
    "with natural pacing. Open with a one-line orientation of what the meeting was "
    "about, then walk through the key points, decisions, and action items as if "
    "telling a colleague what happened. Keep it tight — about 120-220 words "
    "(~45-90 seconds of speech). Output ONLY the narration text, no preamble."
)


async def _build_spoken_script(
    registry: ProviderRegistry,
    org_id: int,
    summary_text: str,
) -> str:
    """Qwen-generated SPOKEN narration of the summary for the Kokoro vocal
    summary — distinct from the written summary, shaped for the ear."""
    llm = registry.get_llm(org_id, task="quality")
    user_prompt = (
        "Meeting summary to narrate aloud:\n\n"
        + summary_text.strip()
        + "\n\nWrite the spoken narration now."
    )
    try:
        raw = await llm.chat(
            SPOKEN_SUMMARY_SYSTEM_PROMPT,
            user_prompt,
            max_tokens=600,
            temperature=0.6,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Spoken-summary script generation failed: %s", exc)
        return ""
    return (raw or "").strip()


def _normalize_speaker_ids(
    script: list[dict],
    *,
    host_voice: str = "alice",
    analyst_voice: str = "frank",
) -> dict[str, str]:
    """Constrain script speakers to host/analyst (the only two voices the
    podcast format renders). Returns the speaker -> voice mapping."""
    speakers = {"host": host_voice, "analyst": analyst_voice}
    for t in script:
        sid = t.get("speaker_id", "host").strip().lower()
        if sid not in speakers:
            t["speaker_id"] = (
                "analyst"
                if sid in ("analyst", "guest", "expert", "speaker_2", "speaker2")
                else "host"
            )
    return speakers


@router.post("/api/sessions/{session_id}/tts/podcast")
async def synthesize_podcast(
    session_id: str,
    response: Response,
    format: str = Query("mp3", pattern="^(mp3|wav)$"),
    host_voice: Optional[str] = Query(None),
    analyst_voice: Optional[str] = Query(None),
    regenerate: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Render a two-voice podcast TTS of the session summary.

    v3.18.3 background-jobification: cache-hit returns 200. Cache-miss
    enqueues an arq job and returns 202 + `{job_id, status_url}`. The
    frontend polls `/api/jobs/<id>` for completion (`result.script` +
    `result.speakers` are included in the job result so the UI can show
    the script as soon as the worker finishes).
    """
    gate_feature_for_caller(current_user, "qwen36_summary", active_org)  # v3.29.0 moat fix: podcast TTS runs the server LLM + TTS
    session = _resolve_session(session_id, db, active_org.organization.id, current_user, min_level="edit")
    session_id = session.id  # normalize to numeric ID for downstream helpers
    text = _summary_text(session)
    if not text:
        raise HTTPException(status_code=400, detail="Session has no summary or transcript to convert.")

    registry = ProviderRegistry(db)
    provider = registry.get_tts(active_org.organization.id)
    if not getattr(provider, "supports_podcast", False):
        return JSONResponse(
            status_code=501,
            content={
                "detail": f"TTS provider '{getattr(provider, 'name', 'unknown')}' does not "
                "support multi-voice podcast generation. Switch to VibeVoice in Provider "
                "Settings."
            },
        )

    out_path = _output_path(active_org.organization.slug, session_id, "podcast", format)
    script_path = _output_dir(active_org.organization.slug, session_id) / "podcast_script.json"
    cached = out_path.exists() and script_path.exists() and not regenerate

    if cached:
        script_payload = json.loads(script_path.read_text() or "[]")
        speakers = {"host": host_voice or "alice", "analyst": analyst_voice or "frank"}
        return PodcastTTSResponse(
            session_id=session_id,
            provider=getattr(provider, "name", "unknown"),
            format=format,
            bytes=out_path.stat().st_size,
            cached=True,
            audio_url=f"/api/sessions/{session_id}/tts/podcast.{format}",
            script=[PodcastTurn(**t) for t in script_payload],
            speakers=speakers,
        )

    # v3.18.3: enqueue + 202. Fall back to inline if arq is unavailable.
    try:
        from services.job_runner import enqueue_job
        job_id = await enqueue_job(
            "render_tts_podcast_job",
            active_org.organization.id,
            active_org.organization.slug,
            session_id,
            host_voice,
            analyst_voice,
            format,
            owner_user_id=current_user.id,
            owner_org_id=active_org.organization.id,
        )
        response.status_code = 202
        return {
            "job_id": job_id,
            "status_url": f"/api/jobs/{job_id}",
            "session_id": session_id,
            "provider": getattr(provider, "name", "unknown"),
            "format": format,
            "cached": False,
            "audio_url": f"/api/sessions/{session_id}/tts/podcast.{format}",
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("synthesize_podcast: arq enqueue failed: %s — running inline", exc)

    script = await _build_podcast_script(registry, active_org.organization.id, text)
    if not script:
        raise HTTPException(status_code=502, detail="Could not generate a podcast script.")

    speakers = _normalize_speaker_ids(
        script,
        host_voice=host_voice or "alice",
        analyst_voice=analyst_voice or "frank",
    )

    try:
        audio_bytes = await provider.synthesize_podcast(script, speakers, format=format)
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="Active TTS provider does not implement podcast mode.")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Podcast synth failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"TTS provider error: {exc}")
    if not audio_bytes:
        raise HTTPException(status_code=502, detail="TTS provider returned empty audio.")
    out_path.write_bytes(audio_bytes)
    script_path.write_text(json.dumps(script))

    return PodcastTTSResponse(
        session_id=session_id,
        provider=getattr(provider, "name", "unknown"),
        format=format,
        bytes=out_path.stat().st_size,
        cached=False,
        audio_url=f"/api/sessions/{session_id}/tts/podcast.{format}",
        script=[PodcastTurn(**t) for t in script],
        speakers=speakers,
    )


@router.get("/api/sessions/{session_id}/tts/podcast.{ext}")
async def get_podcast_audio(
    session_id: str,
    ext: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if ext not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="Unsupported format")
    session = _resolve_session(session_id, db, active_org.organization.id, current_user)
    session_id = session.id  # normalize to numeric ID for downstream helpers
    out_path = _output_path(active_org.organization.slug, session_id, "podcast", ext)
    if not out_path.exists():
        raise HTTPException(status_code=404, detail="Podcast audio not yet generated")
    return FileResponse(
        path=str(out_path),
        media_type=_media_type_for(ext),
        filename=f"podcast-{session_id}.{ext}",
    )


@router.get("/api/sessions/{session_id}/tts/podcast/script")
async def get_podcast_script(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Return the cached host/analyst script for this session's podcast recap.

    Lets the UI show the script as a stable artifact (with copy/download
    affordance) without re-running TTS or LLM. Returns 404 when no podcast has
    been generated yet for this session in this org.
    """
    session = _resolve_session(session_id, db, active_org.organization.id, current_user)
    session_id = session.id  # normalize to numeric ID for downstream helpers
    script_path = _output_dir(active_org.organization.slug, session_id) / "podcast_script.json"
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="No podcast script for this session yet.")
    try:
        payload = json.loads(script_path.read_text() or "[]")
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Stored podcast script is corrupt.")
    return {
        "session_id": session_id,
        "script": [PodcastTurn(**t).model_dump() for t in payload],
        "generated_at": datetime.fromtimestamp(script_path.stat().st_mtime, tz=timezone.utc).isoformat(),
    }


# ---------- async TTS jobs ----------


class TtsJobResponse(BaseModel):
    job_id: str
    session_id: int
    kind: str
    stage: str
    progress_pct: int
    audio_url: Optional[str] = None
    error_message: Optional[str] = None


def _tts_job_response(job) -> TtsJobResponse:
    return TtsJobResponse(
        job_id=str(job.job_id),
        session_id=job.session_id,
        kind=job.kind,
        stage=job.stage,
        progress_pct=job.progress_pct,
        audio_url=(
            f"/api/sessions/{job.session_id}/tts/{job.kind}.{job.format}"
            if job.stage == "done"
            else None
        ),
        error_message=job.error_message,
    )


@router.post("/api/sessions/{session_id}/tts/summary/start", response_model=TtsJobResponse)
async def start_summary_job(
    session_id: str,
    voice: Optional[str] = Query(None),
    format: str = Query("mp3", pattern="^(mp3|wav)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Enqueue an async summary TTS job. Returns immediately with job_id; the
    caller subscribes to /ws/tts/{job_id} for progress events. Pre-renders to
    the same path as the sync endpoint so cached audio remains compatible."""
    gate_feature_for_caller(current_user, "qwen36_summary", active_org)  # v3.29.0 moat fix: vocal summary runs the server LLM + TTS
    from database.models import TtsJob
    from services.tts_jobs import tts_queue, ws_manager
    import uuid as _uuid

    session = _resolve_session(session_id, db, active_org.organization.id, current_user, min_level="edit")
    session_id = session.id  # normalize to numeric ID for downstream helpers
    if not _summary_text(session):
        raise HTTPException(status_code=400, detail="Session has no summary or transcript to read aloud.")

    job = TtsJob(
        organization_id=active_org.organization.id,
        user_id=current_user.id,
        job_id=_uuid.uuid4(),
        session_id=session_id,
        kind="summary",
        voice=voice,
        format=format,
        stage="queued",
        progress_pct=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    await tts_queue.enqueue(str(job.job_id))
    return _tts_job_response(job)


@router.post("/api/sessions/{session_id}/tts/podcast/start", response_model=TtsJobResponse)
async def start_podcast_job(
    session_id: str,
    host_voice: Optional[str] = Query(None),
    analyst_voice: Optional[str] = Query(None),
    format: str = Query("mp3", pattern="^(mp3|wav)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Enqueue an async podcast TTS job. Returns immediately with job_id; the
    caller subscribes to /ws/tts/{job_id} for progress events. Returns 501
    when the active provider does not support multi-voice synthesis."""
    gate_feature_for_caller(current_user, "qwen36_summary", active_org)  # v3.29.0 moat fix: podcast TTS runs the server LLM + TTS
    from database.models import TtsJob
    from services.tts_jobs import tts_queue
    import uuid as _uuid

    session = _resolve_session(session_id, db, active_org.organization.id, current_user, min_level="edit")
    session_id = session.id  # normalize to numeric ID for downstream helpers
    if not _summary_text(session):
        raise HTTPException(status_code=400, detail="Session has no summary or transcript to convert.")

    registry = ProviderRegistry(db)
    provider = registry.get_tts(active_org.organization.id)
    if not getattr(provider, "supports_podcast", False):
        return JSONResponse(
            status_code=501,
            content={
                "detail": (
                    f"TTS provider '{getattr(provider, 'name', 'unknown')}' does not "
                    "support multi-voice podcast generation. Switch to VibeVoice in Provider Settings."
                )
            },
        )

    job = TtsJob(
        organization_id=active_org.organization.id,
        user_id=current_user.id,
        job_id=_uuid.uuid4(),
        session_id=session_id,
        kind="podcast",
        host_voice=host_voice,
        analyst_voice=analyst_voice,
        format=format,
        stage="queued",
        progress_pct=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    await tts_queue.enqueue(str(job.job_id))
    return _tts_job_response(job)


@router.get("/api/tts/jobs/{job_id}", response_model=TtsJobResponse)
async def get_tts_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Status snapshot for an async TTS job."""
    from database.models import TtsJob
    import uuid as _uuid

    try:
        job = (
            db.query(TtsJob)
            .filter(
                TtsJob.job_id == _uuid.UUID(job_id),
                TtsJob.organization_id == active_org.organization.id,
            )
            .first()
        )
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid job_id.")
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.user_id != current_user.id and not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Job belongs to another user.")
    return _tts_job_response(job)


@ws_router.websocket("/ws/tts/{job_id}")
async def tts_progress_websocket(websocket: WebSocket, job_id: str):
    """Live stage / progress events for an async TTS job. The client connects
    after POST /tts/{kind}/start and receives a JSON payload on each state
    transition until stage in {done, failed}. Auth here is the same connection
    upgrade flow as upload progress: oauth2-proxy already validated the
    cookie at the reverse proxy."""
    from services.tts_jobs import ws_manager
    import uuid as _uuid

    ok, user = await enforce_ws_auth(websocket)
    if not ok:
        return

    try:
        _uuid.UUID(job_id)
    except (ValueError, TypeError):
        await websocket.close(code=1008, reason="invalid job_id")
        return

    from database.database import SessionLocal
    from database.models import TtsJob
    db = SessionLocal()
    try:
        query = db.query(TtsJob).filter(TtsJob.job_id == _uuid.UUID(job_id))
        if user is not None and not getattr(user, "is_superuser", False):
            query = query.filter(
                TtsJob.organization_id.in_(getattr(user, "_org_ids", [])),
                TtsJob.user_id == user.id,
            )
        job = query.first()
        if not job:
            await websocket.close(code=1008, reason="job not found")
            return
        from services.tts_jobs import _job_payload
        initial_payload = _job_payload(job)
    finally:
        db.close()

    await ws_manager.connect(job_id, websocket)
    try:
        # Send the current state immediately so the client doesn't wait for
        # the next event to render initial progress.
        await websocket.send_json(initial_payload)
        # Keep the socket open; the worker pool broadcasts via ws_manager.
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(job_id, websocket)
