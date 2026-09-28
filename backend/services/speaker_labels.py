"""Speaker-label normalization — no raw diarizer codes anywhere user-visible.

pyannote emits machine labels (``SPEAKER_00``, ``SPEAKER_01``, …). After
``identify_speakers`` matches enrolled voices, MATCHED labels become real
display names — but unmatched ones used to leak raw codes into the summary
prompt, the stored summary, and the transcript UI ("the builder's
representative (SPEAKER_02)…"). This module makes the labels humane and
consistent everywhere:

  - real names stay untouched,
  - raw diarizer codes become "Speaker 1", "Speaker 2", … (stable, ordered
    by first appearance in the meeting),
  - the original code is preserved in ``raw_label`` so the manual naming /
    enrollment flow (api/speakers.py keys on raw_label first) keeps working,
  - the ``transcriptions`` DB rows (what the transcript UI renders) are
    re-synced from the final diarized segments so the UI shows the same
    names/labels as the summary instead of "unknown".

Pure helpers (no DB) are exposed separately so the summarizer can build a
speaker-attributed prompt even for sessions that were never normalized.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Raw diarizer codes: SPEAKER_00 / speaker 3 / SPEAKER-2 …
_RAW_LABEL_RE = re.compile(r"^SPEAKER[\s_-]?\d+$", re.IGNORECASE)
# Our own normalized form — must be treated as already-normalized (and its
# number reserved) so re-running normalization is a stable no-op.
_NORMALIZED_RE = re.compile(r"^Speaker (\d+)$")


def is_raw_label(label: Optional[str]) -> bool:
    """True for machine diarizer codes (SPEAKER_00 …), False for real names
    AND for our already-normalized "Speaker N" form."""
    if not label:
        return False
    label = label.strip()
    if _NORMALIZED_RE.match(label):
        return False
    return bool(_RAW_LABEL_RE.match(label))


def normalized_label_map(labels: list[str]) -> dict[str, str]:
    """Map raw diarizer codes -> "Speaker N" (first-appearance order).

    Real names map to themselves; existing "Speaker N" labels keep their
    number (reserved so new assignments never collide). Deterministic and
    idempotent: running it on its own output changes nothing.
    """
    mapping: dict[str, str] = {}
    used: set[int] = set()
    for label in labels:
        m = _NORMALIZED_RE.match((label or "").strip())
        if m:
            used.add(int(m.group(1)))
    next_n = 1
    for label in labels:
        clean = (label or "").strip()
        if not clean or clean in mapping:
            continue
        if is_raw_label(clean):
            while next_n in used:
                next_n += 1
            mapping[clean] = f"Speaker {next_n}"
            used.add(next_n)
        else:
            mapping[clean] = clean
    return mapping


def build_attributed_transcript(
    segments: list[dict[str, Any]],
    label_map: Optional[dict[str, str]] = None,
) -> tuple[str, list[str]]:
    """Render diarized segments as a speaker-attributed transcript.

    Returns ``(text, speakers)`` where text is "Name: utterance" lines with
    consecutive same-speaker segments merged, and speakers is the distinct
    display labels in first-appearance order. This is what gives the
    summarizer REAL per-statement attribution instead of a flat wall of
    text + a "guess who said what" instruction.
    """
    if label_map is None:
        label_map = normalized_label_map(
            [s.get("speaker") or "" for s in segments]
        )
    lines: list[str] = []
    speakers: list[str] = []
    cur_speaker: Optional[str] = None
    cur_parts: list[str] = []

    def _flush() -> None:
        if cur_parts:
            text = " ".join(cur_parts).strip()
            if text:
                lines.append(f"{cur_speaker}: {text}" if cur_speaker else text)

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        raw = (seg.get("speaker") or "").strip()
        display = label_map.get(raw, raw) or "Speaker"
        if display not in speakers:
            speakers.append(display)
        if display != cur_speaker:
            _flush()
            cur_speaker = display
            cur_parts = [text]
        else:
            cur_parts.append(text)
    _flush()
    return "\n".join(lines), speakers


def normalize_session_speaker_labels(session, db) -> dict[str, str]:
    """Persist normalized labels onto a session after diarize + identify.

    - rewrites ``transcript_diarized.segments[*].speaker`` (raw codes ->
      "Speaker N"), stashing the original in ``raw_label`` when not already
      set (so manual naming/enrollment still resolves the diarizer label),
    - refreshes the top-level ``speakers`` list,
    - re-syncs the ``transcriptions`` rows (the transcript the UI renders)
      from the final segments so per-line speakers match the summary.

    Best-effort by design: returns the applied mapping ({} when there was
    nothing to do). Caller wraps in try/except — label polish must never
    fail a finalize/reprocess.
    """
    diarized = getattr(session, "transcript_diarized", None)
    if not isinstance(diarized, dict):
        return {}
    segments = diarized.get("segments") or []
    if not segments:
        return {}

    labels = [(s.get("speaker") or "").strip() for s in segments]
    mapping = normalized_label_map([l for l in labels if l])
    changed = {k: v for k, v in mapping.items() if k != v}

    if changed:
        new_segments = []
        for seg in segments:
            raw = (seg.get("speaker") or "").strip()
            if raw in changed:
                new_seg = {**seg, "speaker": changed[raw]}
                new_seg.setdefault("raw_label", raw)
            else:
                new_seg = seg
            new_segments.append(new_seg)
        diarized["segments"] = new_segments
        segments = new_segments

    seen: list[str] = []
    for seg in segments:
        spk = (seg.get("speaker") or "").strip()
        if spk and spk not in seen:
            seen.append(spk)
    diarized["speakers"] = seen
    session.transcript_diarized = diarized
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(session, "transcript_diarized")

    # Re-sync the transcript rows the UI reads. Only when the segments carry
    # text (they always do on the server pipeline); guarded so a malformed
    # session can't wipe its transcript. No FK references transcriptions.id.
    try:
        texted = [s for s in segments if (s.get("text") or "").strip()]
        if texted:
            from database.models import Transcription
            db.query(Transcription).filter(
                Transcription.session_id == session.id
            ).delete()
            for seg in texted:
                db.add(Transcription(
                    session_id=session.id,
                    text=(seg.get("text") or "").strip(),
                    speaker=(seg.get("speaker") or None),
                    start_time=float(seg.get("start") or 0),
                    end_time=float(seg.get("end") or 0),
                    confidence=float(seg.get("confidence") or 0.95),
                ))
    except Exception:  # noqa: BLE001
        logger.exception(
            "normalize_session_speaker_labels: transcription resync failed "
            "session=%s (labels still normalized)", session.id,
        )

    db.commit()
    if changed:
        logger.info(
            "normalize_session_speaker_labels: session=%s relabeled %s",
            session.id, changed,
        )
    return mapping
