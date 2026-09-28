"""Server-side summary-slice persistence for room recordings.

Browser always-on sessions store live-summary slices in client state so each
user sees their own running summary. That works because one user owns the
browser tab.

Conference-room sessions don't have a privileged user — multiple admins
+ viewers + after-the-fact reviewers all need to see the same slice
stack. So we roll + persist the slices on the server.

Storage
-------
We append slices onto ``RecordingSession.processing_metadata['summary_slices']``
(JSONB list). No schema migration needed; the list is queryable via JSONB
operators if we ever want to lift it into its own table later.

A slice looks like::

    {
      "id": "<uuid4>",
      "text": "...",
      "word_count": 312,             # absolute transcript words at slice time
      "word_range_start": 0,         # words BEFORE this slice
      "word_range_end": 312,         # words covered by this slice
      "model": "Qwen 3.6 35B-A3B",
      "provider": "LiteLLMProvider",
      "created_at": "2026-05-20T15:42:00+00:00",
      "triggered_by": "room-recorder" | "auto-words" | "manual"
    }

Triggers
--------
``maybe_auto_trigger_slice`` is called from the always-on chunks endpoint
after each chunk lands. When the transcript has grown by
``ROOM_SLICE_TRIGGER_WORDS`` (default 500) since the last slice, we
generate a new one — for both room sessions AND browser always-on sessions.

History note: previously only room sessions auto-triggered (browser
always-on did its own rolling client-side using Qwen 3 0.6B INT8 +
previous-summary context). That tiny model choked on incremental
summarization and the slice stack would repeat early content. We moved
browser always-on onto the same server-side path so it gets Qwen 3.6
35B-A3B-Vision quality — the gate that excluded ``session.room_id is None``
was removed 2026-05-21.

``generate_slice`` is the single entry point for actually producing a
slice (auto or manual). It calls the per-org LLM provider with the same
prompt the always-on ``/summarize`` endpoint uses, persists, and returns
the slice dict.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from database.models import RecordingSession

logger = logging.getLogger(__name__)


# Word delta that triggers an auto-generated slice. Mirrors the browser
# always-on default; configurable via env so an op can dial it up/down
# without code changes. 500 is roughly 3 minutes of conversation.
ROOM_SLICE_TRIGGER_WORDS = int(os.getenv("ROOM_SLICE_TRIGGER_WORDS", "500"))

# Bound the slice list so a marathon session doesn't blow out the JSONB
# column. 200 slices = 100k words at the default threshold.
MAX_SLICES_PER_SESSION = int(os.getenv("ROOM_SLICE_MAX_PER_SESSION", "200"))


SLICE_SYSTEM_PROMPT = (
    "You are a live meeting summarizer. The user is currently in a meeting and "
    "wants a running summary that updates as new conversation happens. Given the "
    "transcript so far and the previous summary, produce an updated summary that:\n"
    "- Leads with what is NEW since the previous summary\n"
    "- Preserves key context, decisions, and action items from before\n"
    "- Stays under 200 words\n"
    "- Reads naturally, like notes a thoughtful attendee would take\n\n"
    "Do not list speakers verbatim. Do not repeat the previous summary word-for-word."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _count_words(text: Optional[str]) -> int:
    if not text:
        return 0
    return len(text.split())


def get_slices(session: RecordingSession) -> list[dict[str, Any]]:
    """Return the persisted slice list for a session (oldest first).

    Defensive: ``processing_metadata`` may be ``None`` on legacy rows; the
    ``summary_slices`` key may be missing or not a list. We tolerate all
    of those and return ``[]``.
    """
    metadata = session.processing_metadata or {}
    slices = metadata.get("summary_slices")
    if not isinstance(slices, list):
        return []
    # Sort by created_at so consumers don't have to. Missing/unparseable
    # timestamps sink to the end of the list rather than crashing.
    def _sort_key(s: dict[str, Any]) -> str:
        return str(s.get("created_at") or "")
    return sorted(slices, key=_sort_key)


def _last_slice_word_end(session: RecordingSession) -> int:
    """Return the ``word_range_end`` of the most-recent slice (0 if none)."""
    slices = get_slices(session)
    if not slices:
        return 0
    last = slices[-1]
    val = last.get("word_range_end")
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _previous_summary_text(session: RecordingSession) -> Optional[str]:
    slices = get_slices(session)
    if not slices:
        return None
    return slices[-1].get("text")


def _slice_user_prompt(transcript: str, previous_summary: Optional[str]) -> str:
    tail = transcript[-60000:]
    if previous_summary and previous_summary.strip():
        return (
            "Previous running summary:\n"
            f"{previous_summary.strip()}\n\n"
            "New transcript since that summary (most recent at the bottom):\n"
            f"{tail}"
        )
    return (
        "Transcript so far (most recent at the bottom):\n"
        f"{tail}"
    )


def _append_slice(session: RecordingSession, slice_obj: dict[str, Any]) -> None:
    """Persist a slice onto ``processing_metadata['summary_slices']``.

    Caller is responsible for ``db.commit()``. We update the in-memory
    object + ``flag_modified`` so the JSONB column actually re-serializes
    on flush (SQLAlchemy dirty-check needs the explicit hint).
    """
    metadata = dict(session.processing_metadata or {})
    slices = list(metadata.get("summary_slices") or [])
    slices.append(slice_obj)
    if len(slices) > MAX_SLICES_PER_SESSION:
        # Keep the most recent slices — the older ones can be derived from
        # the transcript if a reviewer ever needs them, but we never want
        # the live UI to start dropping rows.
        slices = slices[-MAX_SLICES_PER_SESSION:]
    metadata["summary_slices"] = slices
    metadata["summary_slices_last_at"] = slice_obj.get("created_at")
    session.processing_metadata = metadata
    flag_modified(session, "processing_metadata")


async def generate_slice(
    db: Session,
    session: RecordingSession,
    *,
    triggered_by: str,
    organization_id: Optional[int] = None,
) -> Optional[dict[str, Any]]:
    """Produce + persist a single summary slice for ``session``.

    Returns the new slice dict on success, ``None`` if there isn't enough
    new transcript to summarize. Raises on LLM failure so the caller can
    surface the error (auto-trigger swallows; manual POST surfaces a 503).
    """
    transcript = (session.transcript_simple or "").strip()
    if not transcript:
        return None

    word_count_now = _count_words(transcript)
    last_word_end = _last_slice_word_end(session)
    if word_count_now <= last_word_end:
        # Nothing new since the last slice.
        return None

    previous_summary = _previous_summary_text(session)
    user_prompt = _slice_user_prompt(transcript, previous_summary)

    org_id = organization_id if organization_id is not None else session.organization_id

    try:
        from services.providers.registry import get_provider_registry

        registry = get_provider_registry(db)
        llm = registry.get_llm(org_id, task="quality")
        text = await llm.chat(
            system_prompt=SLICE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            max_tokens=600,
            temperature=0.3,
        )
    except Exception:
        logger.exception(
            "Slice LLM call failed for session=%s org=%s trigger=%s",
            getattr(session, "id", None),
            org_id,
            triggered_by,
        )
        raise

    text = (text or "").strip()
    if not text:
        logger.warning(
            "Empty slice text for session=%s trigger=%s",
            session.id,
            triggered_by,
        )
        return None

    slice_obj = {
        "id": str(uuid.uuid4()),
        "text": text,
        "word_count": word_count_now,
        "word_range_start": last_word_end,
        "word_range_end": word_count_now,
        "model": getattr(llm, "model", None),
        "provider": llm.__class__.__name__,
        "created_at": _now().isoformat(),
        "triggered_by": triggered_by,
    }
    _append_slice(session, slice_obj)
    return slice_obj


async def maybe_auto_trigger_slice(
    db: Session,
    session: RecordingSession,
    *,
    trigger_words: int = ROOM_SLICE_TRIGGER_WORDS,
) -> Optional[dict[str, Any]]:
    """Generate a slice if the transcript has grown past the trigger threshold.

    Fires for ALL session types — conference-room recordings AND browser
    always-on sessions. Previously gated on ``session.room_id`` because
    browser always-on rolled its own slices client-side with Qwen 3 0.6B
    INT8; that model was too small to do incremental summarization with
    previous-summary context and the slice stack repeated early content.
    Now both surfaces use the server's Qwen 3.6 35B-A3B-Vision provider
    so the live summary stays coherent across a long session.

    The ``triggered_by`` label distinguishes the source — ``room-recorder``
    for room sessions, ``auto-words`` for browser always-on — so analytics
    can still see the split.

    Failures are logged and swallowed so a transient LLM hiccup never
    blocks chunk ingest.
    """
    transcript = session.transcript_simple or ""
    word_count_now = _count_words(transcript)
    last_word_end = _last_slice_word_end(session)
    if word_count_now - last_word_end < trigger_words:
        return None
    triggered_by = "room-recorder" if session.room_id is not None else "auto-words"
    try:
        return await generate_slice(
            db,
            session,
            triggered_by=triggered_by,
        )
    except Exception:
        # Already logged inside generate_slice; we explicitly do NOT
        # re-raise so the chunk POST still returns 200. The next chunk
        # that crosses the threshold will retry.
        return None
