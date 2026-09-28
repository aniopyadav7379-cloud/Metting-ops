"""Shared action-item extraction from the two JSON columns the
summarizer writes into (`final_summary` and `ai_insights`).

Single source of truth used by:
 - api.uploads (post-meeting summarizer)
 - api.simple_recording_db (final-recording finalize path)
 - services.auto_summarization_service (always-on/live summarizer)
 - api.satellite_api (satellite-finalize path)
 - alembic.021_action_items (backfill on migrate)

Mirrors `frontend/src/components/dashboard/actionItems.ts` so the
new `action_items` table contains the same items the dashboard parser
used to surface client-side, and the dashboard can flip to a
straight DB read without losing rows.

Returns a list of dicts with the shape consumed by the new
`action_items` table:

    {
        "text": str,                # required, non-empty
        "owner": str | None,
        "due_date": datetime | None,
        "source": "final_summary" | "ai_insights",
        "raw_payload": dict,        # original LLM blob for audit
    }
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional


logger = logging.getLogger(__name__)


# Keys we will look at inside a bucket dict, in priority order. The
# summarizer is inconsistent: sometimes `actions`, sometimes
# `action_items`, sometimes (older) `tasks` / `action-items`.
_LIST_KEYS = ("action_items", "actions", "tasks", "action-items")


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        for k in ("action", "text", "title", "description", "task"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def _pick_list(bucket: Any) -> List[Any]:
    if not isinstance(bucket, dict):
        return []
    for key in _LIST_KEYS:
        v = bucket.get(key)
        if isinstance(v, list) and v:
            return v
    return []


def _coerce_owner(raw: Any) -> Optional[str]:
    if not isinstance(raw, dict):
        return None
    for k in ("owner", "assignee", "assigned_to", "responsible"):
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    return None


_REL_DAY_RX = re.compile(
    r"\b(today|tonight|tomorrow|next\s+week|this\s+week|end\s+of\s+(?:week|month))\b",
    re.IGNORECASE,
)


def _coerce_due(raw: Any) -> Optional[datetime]:
    """Best-effort parse of LLM-extracted due strings into a timestamp.

    The summarizer emits "Not specified", "Next Thursday", "Friday",
    ISO dates, sometimes nothing. We accept ISO-shaped strings and
    leave the rest as NULL — the original phrasing is preserved in
    `raw_payload` for the UI to render verbatim if it wants."""
    if not isinstance(raw, dict):
        return None
    for k in ("due_date", "dueDate", "deadline", "due"):
        v = raw.get(k)
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s or s.lower() in {"not specified", "n/a", "tbd", "none", "null"}:
            continue
        # Try ISO 8601 first; tolerate trailing Z.
        try:
            iso = s[:-1] + "+00:00" if s.endswith("Z") else s
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass
        # Date-only YYYY-MM-DD
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        # Relative phrases get left as NULL; we keep the string in
        # raw_payload so the UI can render it as fallback copy.
        if _REL_DAY_RX.search(s):
            return None
    return None


def _iter_buckets(
    final_summary: Any, ai_insights: Any
) -> Iterable[tuple[str, dict]]:
    """Yield (source_label, bucket_dict) pairs to scan in order.

    Order matters for dedup: we keep the first occurrence of a given
    text key. final_summary wins over ai_insights because the
    summarizer writes structured `actions` with owner/priority there,
    while ai_insights tends to mirror the same items in a slightly
    different shape."""
    if isinstance(final_summary, dict):
        yield "final_summary", final_summary
    if isinstance(ai_insights, dict):
        yield "ai_insights", ai_insights


def extract_action_items(
    final_summary: Any,
    ai_insights: Any,
) -> List[dict]:
    """Return a deduplicated, ordered list of action-item dicts ready
    for INSERT into the `action_items` table.

    Dedup key is the first 120 chars of the text (lowercased + collapsed
    whitespace), matching the frontend parser's behavior."""
    out: List[dict] = []
    seen: set[str] = set()

    for source_label, bucket in _iter_buckets(final_summary, ai_insights):
        for raw in _pick_list(bucket):
            text = _coerce_text(raw)
            if not text:
                continue
            key = re.sub(r"\s+", " ", text.lower())[:120]
            if key in seen:
                continue
            seen.add(key)

            owner = _coerce_owner(raw)
            due = _coerce_due(raw)
            payload: dict = raw if isinstance(raw, dict) else {"text": text}

            out.append(
                {
                    "text": text[:4000],
                    "owner": owner,
                    "due_date": due,
                    "source": source_label,
                    "raw_payload": payload,
                }
            )

    return out


def persist_action_items(db, session) -> int:
    """Replace the persisted action items for a session with a fresh
    extraction from its `final_summary` and `ai_insights` columns.

    Called after every summarizer pass. Returns the number of rows
    inserted. Re-process is a fresh slate so we delete-then-insert
    (manual additions made between summarizer passes will be lost —
    that's intentional, the LLM is the source of truth and the user
    can re-add manuals after).

    Caller is responsible for the surrounding commit; this function
    only flushes so foreign-key checks see the deletes.
    """
    # Local import keeps the extractor importable from alembic
    # migrations (which don't have a fully wired models package on
    # the path during early upgrades).
    from database.models import ActionItem

    if session is None or getattr(session, "id", None) is None:
        return 0
    if getattr(session, "organization_id", None) is None:
        # Org-scoping is enforced at the table level; without an org
        # id we can't write rows. Older sessions predate multi-org.
        logger.debug(
            "persist_action_items: skipping session %s with no organization_id",
            getattr(session, "id", "?"),
        )
        return 0

    items = extract_action_items(
        getattr(session, "final_summary", None),
        getattr(session, "ai_insights", None),
    )

    db.query(ActionItem).filter(ActionItem.session_id == session.id).delete(
        synchronize_session=False
    )
    db.flush()

    if not items:
        return 0

    rows = [
        ActionItem(
            session_id=session.id,
            organization_id=session.organization_id,
            text=item["text"],
            owner=item["owner"],
            due_date=item["due_date"],
            status="todo",
            sort_order=idx,
            source=item["source"],
            raw_payload=item["raw_payload"],
        )
        for idx, item in enumerate(items)
    ]
    db.add_all(rows)
    db.flush()
    logger.info(
        "action_items: persisted %d items for session %s", len(rows), session.id
    )
    return len(rows)
