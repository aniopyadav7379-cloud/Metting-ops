"""
Shared tool implementations for the meeting-rag agent.

Each helper is org-scoped (filters every query by ``organization_id``) and
returns JSON-serialisable Python data. The HTTP tool-use loop in
``api/agents.py`` invokes these directly; the standalone FastMCP server in
``mcp/meeting_ops_mcp.py`` is a separate process that talks to the backend
over HTTP and intentionally stays as an HTTP wrapper (it's deployed into
external Claude Desktop instances where direct DB access isn't possible).

The OpenAI ``tools`` schema and ``MEETING_RAG_TOOL_MAP`` dispatch table are
the contract the loop relies on — keep them in lock-step with the helper
signatures.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from auth.models import User
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from database.models import (
    ChatHistory,
    RecordingSession as DBRecordingSession,
    Transcription,
)
from services.graph_augmented_retrieval import (
    augment_meeting_search,
    resolve_graph_augmentation_enabled,
)
from services.agent_actions import propose_action

logger = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 20
_MAX_LIST_LIMIT = 50
_MAX_TRANSCRIPT_CHARS = 24000
_DEFAULT_SEARCH_LIMIT = 5


# ---------------------------------------------------------------------------
# Internal: session resolution + text extraction
# ---------------------------------------------------------------------------

def _resolve_session(
    db: Session, org_id: int, session_id: str
) -> Optional[DBRecordingSession]:
    """Resolve a meeting by UUID session_id, then by integer id — always
    scoped to ``org_id``. Returns ``None`` if the meeting either doesn't
    exist or belongs to a different org."""
    q = db.query(DBRecordingSession).filter(
        DBRecordingSession.organization_id == org_id,
    )
    sess = q.filter(DBRecordingSession.session_id == str(session_id)).first()
    if sess:
        return sess
    try:
        int_id = int(session_id)
    except (ValueError, TypeError):
        return None
    return q.filter(DBRecordingSession.id == int_id).first()


def _extract_transcript_text(session: DBRecordingSession) -> str:
    """Pull the best-available transcript representation off a session row.

    Mirrors ``api/ai_chat.py:_get_transcript_text``: prefer the speaker-
    ATTRIBUTED transcript ("Name: utterance" lines built from the diarized
    segments, raw diarizer codes normalized to "Speaker N") over the flat
    ``transcript_simple``. Without going through ``build_attributed_transcript``
    the RAG agent would quote raw ``SPEAKER_00`` codes for any session that
    never went through the label-normalization pass (satellite/companion
    uploads, legacy rows) — while the per-meeting chat shows "Speaker 1" / real
    names for the same data. Routing both through the one helper keeps them in
    parity and means no raw diarizer code can reach the model.
    """
    diarized = session.transcript_diarized
    if isinstance(diarized, dict):
        segments = diarized.get("segments", [])
        if segments:
            from services.speaker_labels import build_attributed_transcript
            text, _speakers = build_attributed_transcript(segments)
            if text.strip():
                return text

    if session.transcript_simple:
        return session.transcript_simple

    raw = session.transcript
    if raw:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict):
                if "segments" in data:
                    parts = [s.get("text", "") for s in data["segments"] if s.get("text")]
                    if parts:
                        return " ".join(parts)
                if "text" in data:
                    return data["text"]
            return str(data)
        except (json.JSONDecodeError, TypeError):
            return str(raw)
    return ""


def _extract_summary(session: DBRecordingSession) -> dict[str, Any]:
    """Return the structured final_summary if present, else attempt to coerce
    the legacy ``summary`` text field into something useful."""
    fs = session.final_summary
    if isinstance(fs, dict) and fs:
        return fs

    s = session.summary
    if not s:
        return {}
    if isinstance(s, dict):
        return s
    try:
        parsed = json.loads(s)
        if isinstance(parsed, dict):
            return parsed
        return {"executive": str(parsed)}
    except (json.JSONDecodeError, TypeError):
        return {"executive": str(s)}


def _session_brief(session: DBRecordingSession) -> dict[str, Any]:
    """A lightweight, JSON-safe handle for any session — used wherever we
    return a list of matches to the model."""
    return {
        "session_id": session.session_id or str(session.id),
        "internal_id": session.id,
        "title": session.title or session.name or f"Session {session.id}",
        "status": session.status or "unknown",
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "duration_seconds": int(session.duration or 0),
        "has_transcript": bool(
            session.transcript_simple or session.transcript_diarized or session.transcript
        ),
        "has_summary": bool(session.final_summary or session.summary),
    }


def _safe_snippet(text: str, query: str, length: int = 600) -> str:
    """Pull a window of ``text`` around the first occurrence of ``query``,
    falling back to the head of the document. Plain-text only — no HTML.
    The window is big enough (default 600 chars) that the LLM can usually
    answer simple recall questions without a follow-up transcript fetch."""
    if not text:
        return ""
    if query:
        idx = text.lower().find(query.lower())
        if idx >= 0:
            half = length // 2
            start = max(0, idx - half)
            end = min(len(text), idx + half)
            chunk = text[start:end].strip()
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(text) else ""
            return f"{prefix}{chunk}{suffix}"
    return text[:length].strip() + ("…" if len(text) > length else "")


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------

async def search_meetings_impl(
    db: Session,
    org_id: int,
    *,
    query: str,
    limit: int = _DEFAULT_SEARCH_LIMIT,
    graph_augment: Optional[bool] = None,
) -> dict[str, Any]:
    """Hybrid search across this org's meetings.

    Strategy: try semantic search first (existing ``SemanticSearchService``
    on Qdrant), then fall back to a SQL ILIKE scan of title / transcript /
    summary. Empty results are returned cleanly — never raises on missing
    Qdrant collections.
    """
    query = (query or "").strip()
    limit = max(1, min(_MAX_SEARCH_LIMIT, int(limit or _DEFAULT_SEARCH_LIMIT)))
    if not query:
        return {"query": query, "results": [], "match_type": "empty"}

    semantic_hits: list[dict[str, Any]] = []
    try:
        from services.semantic_search_service import SemanticSearchService

        svc = SemanticSearchService()
        for hit in svc.search(query, limit=limit, organization_id=org_id):
            semantic_hits.append({
                "session_id": hit.get("session_id"),
                "title": hit.get("title"),
                "score": hit.get("score"),
                "snippet": (hit.get("snippet") or "")[:240],
                "created_at": hit.get("created_at"),
                "match_type": "semantic",
            })
    except Exception as exc:
        logger.debug("Semantic search unavailable, falling back to SQL: %s", exc)

    if semantic_hits:
        base_payload = {
            "query": query,
            "match_type": "semantic",
            "results": semantic_hits[:limit],
        }
        if not resolve_graph_augmentation_enabled(
            {"graph_augmented_retrieval": graph_augment}
            if graph_augment is not None
            else None
        ):
            return base_payload

        try:
            augmented = await augment_meeting_search(
                db,
                org_id,
                query=query,
                base_results=base_payload["results"],
                limit=limit,
                enabled=True,
            )
            return {
                "query": query,
                "match_type": "graph_augmented",
                "results": augmented["results"],
                "graph": augmented["graph"],
            }
        except Exception as exc:  # noqa: BLE001
            logger.debug("graph augmentation search failed, using semantic base: %s", exc)
            return base_payload

    pattern = f"%{query}%"
    rows = (
        db.query(DBRecordingSession)
        .filter(
            DBRecordingSession.organization_id == org_id,
            or_(
                DBRecordingSession.title.ilike(pattern),
                DBRecordingSession.name.ilike(pattern),
                DBRecordingSession.transcript_simple.ilike(pattern),
                DBRecordingSession.summary.ilike(pattern),
            ),
        )
        .order_by(desc(DBRecordingSession.created_at))
        .limit(limit)
        .all()
    )

    results: list[dict[str, Any]] = []
    for row in rows:
        snippet_src = row.transcript_simple or row.summary or ""
        results.append({
            **_session_brief(row),
            "snippet": _safe_snippet(str(snippet_src), query),
            "match_type": "keyword",
        })

    base_payload = {"query": query, "match_type": "keyword", "results": results}
    if not resolve_graph_augmentation_enabled(
        {"graph_augmented_retrieval": graph_augment}
        if graph_augment is not None
        else None
    ):
        return base_payload

    try:
        augmented = await augment_meeting_search(
            db,
            org_id,
            query=query,
            base_results=base_payload["results"],
            limit=limit,
            enabled=True,
        )
        return {
            "query": query,
            "match_type": "graph_augmented",
            "results": augmented["results"],
            "graph": augmented["graph"],
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph augmentation search failed, using keyword base: %s", exc)
        return base_payload


async def list_meetings_impl(
    db: Session,
    org_id: int,
    *,
    limit: int = 20,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """Return the most-recent meetings for this org, newest first."""
    limit = max(1, min(_MAX_LIST_LIMIT, int(limit or 20)))

    q = db.query(DBRecordingSession).filter(
        DBRecordingSession.organization_id == org_id,
    )
    if status:
        q = q.filter(DBRecordingSession.status == status)
    rows = q.order_by(desc(DBRecordingSession.created_at)).limit(limit).all()

    return {
        "count": len(rows),
        "meetings": [_session_brief(r) for r in rows],
    }


async def get_meeting_details_impl(
    db: Session, org_id: int, *, session_id: str
) -> dict[str, Any]:
    """Return metadata + structured summary for a single meeting."""
    session = _resolve_session(db, org_id, session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found in this organization."}

    summary = _extract_summary(session)
    return {
        **_session_brief(session),
        "summary": {
            "executive": summary.get("executive") or summary.get("executive_summary"),
            "bullets": summary.get("bullets") or summary.get("key_points") or [],
            "decisions": summary.get("decisions") or [],
            "action_items": summary.get("action_items") or summary.get("actions") or [],
        },
        "participants": session.participants or [],
        "tags": list(session.tags or []),
    }


async def get_meeting_transcript_impl(
    db: Session,
    org_id: int,
    *,
    session_id: str,
    max_chars: int = 10000,
) -> dict[str, Any]:
    """Return the transcript text for a single meeting (truncated to
    ``max_chars`` to keep the tool result tractable)."""
    session = _resolve_session(db, org_id, session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found in this organization."}

    max_chars = max(500, min(_MAX_TRANSCRIPT_CHARS, int(max_chars or 10000)))
    transcript = _extract_transcript_text(session)
    if not transcript:
        return {
            **_session_brief(session),
            "transcript": "",
            "truncated": False,
            "note": "No transcript is available for this meeting yet.",
        }

    truncated = len(transcript) > max_chars
    body = transcript[:max_chars] if truncated else transcript
    return {
        **_session_brief(session),
        "transcript": body,
        "transcript_chars": len(transcript),
        "truncated": truncated,
    }


async def chat_with_meeting_impl(
    db: Session,
    org_id: int,
    llm_provider: Any,
    *,
    session_id: str,
    message: str,
) -> dict[str, Any]:
    """Answer a question about a *single* meeting by feeding the transcript
    and summary into the LLM as context. Returns the model's reply plus the
    metadata the calling agent will surface to the user."""
    session = _resolve_session(db, org_id, session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found in this organization."}

    transcript = _extract_transcript_text(session)
    summary = _extract_summary(session)
    if not transcript and not summary:
        return {
            "error": (
                "This meeting hasn't been processed yet — no transcript or "
                "summary is available."
            )
        }

    meeting_name = session.title or session.name or "Untitled Meeting"
    duration = session.duration or 0
    duration_str = f"{int(duration // 60)}m {int(duration % 60)}s" if duration else "unknown"
    date_str = (
        session.created_at.strftime("%Y-%m-%d %H:%M") if session.created_at else "unknown"
    )

    summary_text = ""
    if summary:
        parts = []
        if summary.get("executive") or summary.get("executive_summary"):
            parts.append(f"Executive: {summary.get('executive') or summary.get('executive_summary')}")
        if summary.get("bullets"):
            parts.append("Key Points: " + "; ".join(str(b) for b in summary["bullets"]))
        if summary.get("decisions"):
            parts.append("Decisions: " + "; ".join(str(d) for d in summary["decisions"]))
        summary_text = "\n".join(parts)

    transcript_clip = transcript[: _MAX_TRANSCRIPT_CHARS]
    truncated_note = (
        f"\n\n[Transcript truncated to first {_MAX_TRANSCRIPT_CHARS} characters]"
        if len(transcript) > _MAX_TRANSCRIPT_CHARS
        else ""
    )

    system_prompt = (
        "You are a meeting analyst. Answer using only the meeting transcript "
        "and summary provided below. If the material doesn't contain the "
        "answer, say so. Be specific and concise.\n\n"
        f"Meeting: {meeting_name}\nDate: {date_str}\nDuration: {duration_str}\n\n"
        f"Summary:\n{summary_text or '(no summary)'}\n\n"
        f"Transcript:\n{transcript_clip}{truncated_note}"
    )

    try:
        answer = llm_provider.chat_sync(
            system_prompt=system_prompt,
            user_prompt=message,
            max_tokens=600,
            temperature=0.4,
        )
    except Exception as exc:
        logger.error("chat_with_meeting LLM call failed: %s", exc)
        return {"error": f"LLM unavailable: {exc}"}

    return {
        **_session_brief(session),
        "answer": (answer or "").strip(),
        "has_transcript": bool(transcript),
        "has_summary": bool(summary),
    }


async def ask_about_meetings_impl(
    db: Session,
    org_id: int,
    llm_provider: Any,
    *,
    query: str,
    limit: int = _DEFAULT_SEARCH_LIMIT,
    graph_augment: Optional[bool] = None,
) -> dict[str, Any]:
    """Cross-meeting RAG-style answer. Finds the top matching sessions, packs
    their summaries + transcript excerpts into one prompt, and asks the LLM
    to answer with citations. Designed for the model to use when it wants a
    synthesized answer rather than a list of raw matches."""
    search = await search_meetings_impl(
        db,
        org_id,
        query=query,
        limit=limit,
        graph_augment=graph_augment,
    )
    matches = search.get("results", [])
    if not matches:
        return {
            "query": query,
            "answer": "I couldn't find any meetings related to that question.",
            "sources": [],
        }

    sources: list[dict[str, Any]] = []
    context_blocks: list[str] = []
    graph_bundle = search.get("graph") or {}
    evidence_by_session = graph_bundle.get("evidence_by_session") or {}
    for hit in matches:
        sid = hit.get("session_id") or hit.get("internal_id")
        if sid is None:
            continue
        session = _resolve_session(db, org_id, str(sid))
        if not session:
            continue
        summary = _extract_summary(session)
        title = session.title or session.name or f"Session {session.id}"
        date_str = (
            session.created_at.strftime("%Y-%m-%d") if session.created_at else "unknown"
        )
        excerpt = _extract_transcript_text(session)[:1500]
        bullets = summary.get("bullets") or summary.get("key_points") or []
        summary_blob = summary.get("executive") or summary.get("executive_summary") or ""
        block = (
            f"[Source: {title} | {date_str} | session_id={session.session_id or session.id}]\n"
            f"Summary: {summary_blob}\n"
        )
        if bullets:
            block += "Key Points: " + "; ".join(str(b) for b in bullets[:5]) + "\n"
        if excerpt:
            block += f"Excerpt: {excerpt}\n"
        graph_evidence = evidence_by_session.get(str(session.session_id or session.id))
        if isinstance(graph_evidence, dict) and graph_evidence.get("graph_block"):
            block += f"Graph:\n{graph_evidence['graph_block']}\n"
        context_blocks.append(block)
        sources.append({
            "session_id": session.session_id or str(session.id),
            "title": title,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "score": hit.get("score"),
        })

    if not context_blocks:
        return {
            "query": query,
            "answer": "Matching meetings exist but none have transcript or summary content yet.",
            "sources": sources,
        }

    system_prompt = (
        "You are a meeting analyst. Answer the user's question using ONLY the "
        "meeting excerpts below. Cite the meeting title (in parens) for each "
        "claim. If the excerpts don't contain enough information, say so.\n\n"
        + "\n\n".join(context_blocks)
    )

    try:
        answer = llm_provider.chat_sync(
            system_prompt=system_prompt,
            user_prompt=query,
            max_tokens=600,
            temperature=0.4,
        )
    except Exception as exc:
        logger.error("ask_about_meetings LLM call failed: %s", exc)
        return {
            "query": query,
            "error": f"LLM unavailable: {exc}",
            "sources": sources,
        }

    return {
        "query": query,
        "answer": (answer or "").strip(),
        "sources": sources,
    }


async def get_analytics_impl(
    db: Session,
    org_id: int,
    *,
    time_range: str = "month",
) -> dict[str, Any]:
    """Return aggregate meeting analytics for this org over the given
    window. Mirrors ``/api/analytics/summary`` so the model gets the same
    numbers users see in the dashboard."""
    now = datetime.now(timezone.utc)
    delta = {
        "week": timedelta(days=7),
        "month": timedelta(days=30),
        "quarter": timedelta(days=90),
        "year": timedelta(days=365),
    }.get(time_range, timedelta(days=30))
    start_date = now - delta

    sessions = (
        db.query(DBRecordingSession)
        .filter(
            DBRecordingSession.organization_id == org_id,
            DBRecordingSession.created_at >= start_date,
            DBRecordingSession.status.in_(["completed", "processing"]),
        )
        .all()
    )

    total = len(sessions)
    total_duration = sum(s.duration or 0 for s in sessions)
    avg_duration = total_duration / total if total else 0.0

    speaking_time: list[dict[str, Any]] = []
    total_speakers = 0
    session_ids = [s.id for s in sessions]
    if session_ids:
        rows = (
            db.query(
                Transcription.speaker,
                func.sum(
                    func.coalesce(Transcription.end_time, 0)
                    - func.coalesce(Transcription.start_time, 0)
                ).label("total_time"),
                func.count(Transcription.id).label("segments"),
            )
            .filter(
                Transcription.session_id.in_(session_ids),
                Transcription.speaker.isnot(None),
            )
            .group_by(Transcription.speaker)
            .order_by(desc("total_time"))
            .limit(10)
            .all()
        )
        unique = set()
        for row in rows:
            name = row[0] or "Unknown"
            unique.add(name)
            speaking_time.append({
                "speaker": name,
                "total_seconds": round(float(row[1] or 0), 1),
                "segments": int(row[2] or 0),
            })
        total_speakers = len(unique)

    return {
        "time_range": time_range,
        "window_start": start_date.isoformat(),
        "window_end": now.isoformat(),
        "total_sessions": total,
        "total_duration_seconds": int(total_duration),
        "average_duration_seconds": round(avg_duration, 1),
        "total_speakers": total_speakers,
        "speaking_time": speaking_time,
    }


async def get_meeting_insights_impl(
    db: Session, org_id: int, *, session_id: str
) -> dict[str, Any]:
    """Return previously-generated AI insights for a meeting.

    Reads the cached ``ai_insights`` JSON column populated by the post-meeting
    summarizer (see ``api/ai_insights.py``). Does NOT trigger fresh generation
    — the agent should call ``get_meeting_details`` for the canonical summary
    and only use this tool when the user explicitly asks for sentiment /
    keywords / topics / speaker engagement metrics."""
    session = _resolve_session(db, org_id, session_id)
    if not session:
        return {"error": f"Session '{session_id}' not found in this organization."}

    cached = session.ai_insights
    if not isinstance(cached, dict) or not cached:
        return {
            **_session_brief(session),
            "insights": None,
            "note": (
                "No AI insights have been generated for this meeting yet. "
                "Insights are produced automatically once the post-meeting "
                "summarizer has run."
            ),
        }

    return {
        **_session_brief(session),
        "insights": {
            "summary": cached.get("summary"),
            "keywords": cached.get("keywords") or [],
            "action_items": cached.get("action_items") or [],
            "speaker_insights": cached.get("speaker_insights") or [],
            "sentiment": cached.get("sentiment") or {},
            "topics": cached.get("topics") or [],
            "key_decisions": cached.get("key_decisions") or [],
            "follow_ups": cached.get("follow_ups") or [],
        },
    }


# ---------------------------------------------------------------------------
# OpenAI tools schema + dispatch
# ---------------------------------------------------------------------------

MEETING_RAG_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_meetings",
            "description": (
                "Search across the user's meetings by topic, speaker, "
                "keyword, or any phrase. Returns the top matching sessions "
                "with titles, dates, and excerpt snippets. Use this first "
                "when the user asks about a topic without naming a specific "
                "meeting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language search phrase.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of meetings to return.",
                        "minimum": 1,
                        "maximum": _MAX_SEARCH_LIMIT,
                        "default": _DEFAULT_SEARCH_LIMIT,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_about_meetings",
            "description": (
                "Answer a free-form question by retrieving the top matching "
                "meetings and synthesising across them. Use when the user "
                "wants a direct answer rather than a list of meetings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The user's question.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_SEARCH_LIMIT,
                        "default": _DEFAULT_SEARCH_LIMIT,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_meetings",
            "description": (
                "List the user's most recent meetings, newest first. "
                "Optionally filter by status (e.g. 'completed', 'recording'). "
                "Use when the user asks 'what meetings do I have' or 'show "
                "me recent meetings'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_LIST_LIMIT,
                        "default": 20,
                    },
                    "status": {
                        "type": "string",
                        "description": (
                            "Optional status filter such as 'completed', "
                            "'recording', or 'active'."
                        ),
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meeting_details",
            "description": (
                "Fetch full metadata + structured summary (executive summary, "
                "key points, decisions, action items) for one meeting. Pass "
                "the session_id you got from search_meetings or list_meetings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Session UUID or integer id of the meeting."
                        ),
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meeting_transcript",
            "description": (
                "Fetch the transcript text for one meeting. Use when the user "
                "asks for quotes, exact wording, or details that aren't in "
                "the summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID or integer id.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Maximum transcript characters to return "
                            "(default 10000)."
                        ),
                        "minimum": 500,
                        "maximum": _MAX_TRANSCRIPT_CHARS,
                        "default": 10000,
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat_with_meeting",
            "description": (
                "Ask a focused question about ONE specific meeting. The tool "
                "loads that meeting's transcript and summary as context and "
                "returns an answer. Use when you already know which meeting "
                "the user is asking about."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID or integer id.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The question about the meeting.",
                    },
                },
                "required": ["session_id", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_analytics",
            "description": (
                "Return aggregate meeting statistics (counts, durations, top "
                "speakers) over a time range. Use for 'how many meetings did "
                "I have this month' style questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "time_range": {
                        "type": "string",
                        "enum": ["week", "month", "quarter", "year"],
                        "default": "month",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_meeting_insights",
            "description": (
                "Return cached AI insights (keywords, topics, sentiment, "
                "speaker engagement) for one meeting. Use when the user asks "
                "about sentiment, topics, or per-speaker engagement metrics. "
                "If insights aren't cached, this returns a note — do not "
                "regenerate them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session UUID or integer id.",
                    },
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_create_session",
            "description": (
                "Propose creating a new meeting session. Returns a "
                "confirmation request; do not imply the session already "
                "exists until the user confirms."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_rename_session",
            "description": (
                "Propose renaming a specific meeting session. Returns a "
                "confirmation request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["session_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_add_tag",
            "description": (
                "Propose adding a single tag to a session. Returns a "
                "confirmation request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["session_id", "tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_remove_tag",
            "description": (
                "Propose removing a single tag from a session. Returns a "
                "confirmation request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "tag": {"type": "string"},
                },
                "required": ["session_id", "tag"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_trigger_reprocess",
            "description": (
                "Propose triggering a server reprocess for a session. "
                "Returns a confirmation request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "required": ["session_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_draft_followup_email",
            "description": (
                "Propose a drafted follow-up email for a session. Returns "
                "a confirmation request and the drafted text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "recipient_name": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["session_id"],
            },
        },
    },
]


# Tools that need the LLM provider (for nested generation) are flagged so the
# dispatcher knows to thread it through. Keeping this declarative beats
# trying to inspect helper signatures at runtime.
_TOOLS_NEEDING_LLM = {"ask_about_meetings", "chat_with_meeting"}
_TOOLS_USING_GRAPH_AUGMENT = {"search_meetings", "ask_about_meetings"}
_TOOLS_USING_AGENT_ACTIONS = {
    "propose_create_session",
    "propose_rename_session",
    "propose_add_tag",
    "propose_remove_tag",
    "propose_trigger_reprocess",
    "propose_draft_followup_email",
}

_AGENT_ACTION_TOOL_TO_ACTION = {
    "propose_create_session": "create_session",
    "propose_rename_session": "rename_session",
    "propose_add_tag": "add_tag",
    "propose_remove_tag": "remove_tag",
    "propose_trigger_reprocess": "trigger_reprocess",
    "propose_draft_followup_email": "draft_followup_email",
}


MEETING_RAG_TOOL_MAP: dict[str, Callable[..., Any]] = {
    "search_meetings": search_meetings_impl,
    "ask_about_meetings": ask_about_meetings_impl,
    "list_meetings": list_meetings_impl,
    "get_meeting_details": get_meeting_details_impl,
    "get_meeting_transcript": get_meeting_transcript_impl,
    "chat_with_meeting": chat_with_meeting_impl,
    "get_analytics": get_analytics_impl,
    "get_meeting_insights": get_meeting_insights_impl,
}


async def execute_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    db: Session,
    org_id: int,
    current_user: Optional[User] = None,
    llm_provider: Any,
    graph_augment: Optional[bool] = None,
) -> dict[str, Any]:
    """Dispatch a single tool call. Returns a JSON-safe payload — never
    raises; tool errors flow back to the model as ``{"error": "..."}`` so
    it can recover (e.g. try a different session_id)."""
    safe_args = arguments or {}
    if not isinstance(safe_args, dict):
        return {"error": f"Tool arguments must be an object, got {type(safe_args).__name__}."}

    if name in _TOOLS_USING_AGENT_ACTIONS:
        if current_user is None:
            return {"error": f"Tool '{name}' requires an authenticated user context."}
        action_name = _AGENT_ACTION_TOOL_TO_ACTION.get(name)
        if not action_name:
            return {"error": f"Unknown action tool '{name}'."}
        try:
            return await propose_action(
                db=db,
                user=current_user,
                org_id=org_id,
                action=action_name,
                payload=safe_args,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent action proposal failed")
            return {"error": f"{name} failed: {exc}"}

    impl = MEETING_RAG_TOOL_MAP.get(name)
    if impl is None:
        return {"error": f"Unknown tool '{name}'."}

    try:
        extra_kwargs = {}
        if name in _TOOLS_USING_GRAPH_AUGMENT:
            extra_kwargs["graph_augment"] = graph_augment
        if name in _TOOLS_NEEDING_LLM:
            return await impl(
                db,
                org_id,
                llm_provider,
                **safe_args,
                **extra_kwargs,
            )
        return await impl(
            db,
            org_id,
            **safe_args,
            **extra_kwargs,
        )
    except TypeError as exc:
        # Argument-shape mismatch — surface to the model so it can retry.
        logger.warning("Tool %s argument error: %s", name, exc)
        return {"error": f"Bad arguments for {name}: {exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Tool %s execution failed", name)
        return {"error": f"{name} failed: {exc}"}


# ---------------------------------------------------------------------------
# Optional history persistence used by the agent loop
# ---------------------------------------------------------------------------

MEETING_RAG_SESSION_KEY = "__meeting_rag_agent__"


def persist_turn(
    db: Session,
    org_id: int,
    *,
    role: str,
    content: str,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Append a turn to the chat_history table so the agent has memory across
    Dashboard ask-bar invocations."""
    try:
        db.add(ChatHistory(
            session_key=MEETING_RAG_SESSION_KEY,
            role=role,
            content=content,
            metadata_json=json.dumps(metadata) if metadata else None,
            organization_id=org_id,
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to persist meeting-rag turn: %s", exc)
        db.rollback()


def load_recent_history(db: Session, org_id: int, limit: int = 10) -> list[dict[str, str]]:
    rows = (
        db.query(ChatHistory)
        .filter(
            ChatHistory.session_key == MEETING_RAG_SESSION_KEY,
            ChatHistory.organization_id == org_id,
        )
        .order_by(ChatHistory.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [{"role": r.role, "content": r.content} for r in rows]
