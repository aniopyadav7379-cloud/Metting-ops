"""Important Moments extraction (Phase 3 module J).

The LLM identifies candidate moments (decision, action item, key
explanation, question, topic transition, disagreement, conclusion, key
lecture concept) and, for each, a short verbatim quote it believes came
from the transcript. The LLM's own timestamp claim is never trusted
directly — we resolve `timestamp` server-side by finding a real
Transcription row whose text actually contains (a close match of) the
quote. A moment whose quote can't be matched against any real segment gets
timestamp=None rather than a fabricated number: "never invent a source"
applies to timestamps too, not just the moment's existence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from difflib import SequenceMatcher
from typing import List, Optional

from sqlalchemy.orm import Session

from database.models import ImportantMoment, Transcription

logger = logging.getLogger(__name__)

MOMENT_TYPES = (
    "decision",
    "action_item",
    "explanation",
    "question",
    "topic_transition",
    "disagreement",
    "conclusion",
    "key_concept",
)

_SYSTEM_PROMPT = (
    "You identify important moments in a meeting or lecture transcript for "
    "a timeline view. For each moment, give a short VERBATIM quote (5-20 "
    "words copied exactly from the transcript, not paraphrased) so it can "
    "be located. Never invent a moment or quote that isn't in the "
    "transcript. Return ONLY a valid JSON object, no markdown fences."
)


def _build_prompt(transcript: str) -> str:
    types_list = ", ".join(MOMENT_TYPES)
    return (
        f"Identify up to 12 important moments in this transcript. Valid "
        f"moment types: {types_list}.\n\n"
        'Return JSON: {"moments": [{"type": "...", "description": "one '
        'sentence", "quote": "verbatim text from the transcript"}]}\n\n'
        f"{transcript[:16000]}"
    )


def _find_matching_segment(quote: str, transcriptions: List[Transcription]) -> Optional[Transcription]:
    """Find the Transcription row whose text best contains/matches `quote`.
    Exact substring match first (cheapest, most trustworthy); falls back to
    fuzzy similarity only above a high threshold so a near-miss doesn't
    silently attach the wrong timestamp."""
    quote_norm = re.sub(r"\s+", " ", quote.strip().lower())
    if not quote_norm:
        return None

    for t in transcriptions:
        if not t.text:
            continue
        if quote_norm in re.sub(r"\s+", " ", t.text.lower()):
            return t

    best: Optional[Transcription] = None
    best_ratio = 0.0
    for t in transcriptions:
        if not t.text:
            continue
        ratio = SequenceMatcher(None, quote_norm, re.sub(r"\s+", " ", t.text.lower())).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = t
    return best if best_ratio >= 0.6 else None


async def extract_moments(
    db: Session,
    org_id: Optional[int],
    session_id: int,
    transcript: str,
    transcriptions: List[Transcription],
) -> List[ImportantMoment]:
    """Generate and persist ImportantMoment rows for a session. Replaces
    any previously-extracted moments for this session (re-running extract
    is idempotent, matching lectures'/insights' re-generate behavior)."""
    from api.ai_insights import _resolve_llm  # local import: avoid import cycle at module load

    db.query(ImportantMoment).filter(ImportantMoment.session_id == session_id).delete()

    svc = _resolve_llm(db, org_id, task="quality")
    if svc is None:
        db.commit()
        return []

    try:
        raw = await asyncio.to_thread(
            svc.chat_sync,
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_build_prompt(transcript),
            max_tokens=1500,
            temperature=0.3,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Important-moments extraction failed for session=%s: %s", session_id, exc)
        db.commit()
        return []

    if not raw or not raw.strip():
        db.commit()
        return []
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        db.commit()
        return []
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        db.commit()
        return []

    candidates = parsed.get("moments")
    if not isinstance(candidates, list):
        db.commit()
        return []

    rows: List[ImportantMoment] = []
    for cand in candidates[:12]:
        if not isinstance(cand, dict):
            continue
        moment_type = str(cand.get("type") or "").strip().lower()
        description = str(cand.get("description") or "").strip()
        quote = str(cand.get("quote") or "").strip()
        if moment_type not in MOMENT_TYPES or not description:
            continue

        segment = _find_matching_segment(quote, transcriptions) if quote else None
        row = ImportantMoment(
            session_id=session_id,
            organization_id=org_id,
            moment_type=moment_type,
            description=description[:2000],
            quote=quote[:1000] or None,
            timestamp=segment.start_time if segment else None,
            speaker=segment.speaker if segment else None,
        )
        db.add(row)
        rows.append(row)

    db.commit()
    for row in rows:
        db.refresh(row)
    return rows
