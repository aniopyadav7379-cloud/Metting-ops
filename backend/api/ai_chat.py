"""
AI Chat API - Per-meeting AI chat + cross-meeting RAG chat using real LLM inference.
Loads session transcript as context and routes through the per-org ProviderRegistry.
Chat history persisted to PostgreSQL (chat_history table).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import json
import logging
import os

from database.database import get_db
from database.models import RecordingSession as DBRecordingSession, ChatHistory
from auth.dependencies import get_current_organization, get_current_user
from auth.organization import ActiveOrganization
from auth.models import User
from auth.tier import gate_feature_for_caller
from services.providers import get_provider_registry

router = APIRouter(prefix="/api/ai-chat", tags=["AI Chat"])
logger = logging.getLogger(__name__)


def _llm_call(
    db: Session,
    org_id: int,
    *,
    task: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 500,
    temperature: float = 0.7,
) -> str:
    """Resolve the org-aware LLM provider and run a synchronous chat call.

    Raises if registry resolution fails. The caller decides whether to surface
    that to the user as 503 or to handle it; silent fallbacks are not allowed
    because they bypass per-org provider configuration and billing.
    """
    registry = get_provider_registry(db)
    llm = registry.get_llm(org_id=org_id, task=task)
    return llm.chat_sync(
        system_prompt,
        user_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
    )


# ---- Chat history persistence helpers ----

def _load_history(
    db: Session,
    session_key: str,
    organization_id: int,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Load chat history from PostgreSQL, most recent `limit` messages."""
    rows = (
        db.query(ChatHistory)
        .filter(
            ChatHistory.session_key == session_key,
            ChatHistory.organization_id == organization_id,
        )
        .order_by(ChatHistory.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()  # Oldest first
    result = []
    for row in rows:
        entry = {
            "role": row.role,
            "content": row.content,
            "timestamp": row.created_at.isoformat() if row.created_at else datetime.now(timezone.utc).isoformat(),
        }
        if row.metadata_json:
            try:
                entry["metadata"] = json.loads(row.metadata_json)
            except (json.JSONDecodeError, TypeError):
                pass
        result.append(entry)
    return result


def _save_message(
    db: Session,
    session_key: str,
    organization_id: int,
    role: str,
    content: str,
    metadata: Optional[Dict] = None,
):
    """Save a single chat message to PostgreSQL."""
    msg = ChatHistory(
        session_key=session_key,
        role=role,
        content=content,
        metadata_json=json.dumps(metadata) if metadata else None,
        organization_id=organization_id,
    )
    db.add(msg)
    db.commit()


def _clear_history(db: Session, session_key: str, organization_id: int):
    """Delete all chat history for a session key."""
    db.query(ChatHistory).filter(
        ChatHistory.session_key == session_key,
        ChatHistory.organization_id == organization_id,
    ).delete()
    db.commit()


# Request/Response Models
class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    context: Optional[Dict[str, Any]] = {}


class AIResponse(BaseModel):
    content: str
    sources: List[Dict[str, Any]] = []
    metadata: Dict[str, Any] = {}


class ChatMessage(BaseModel):
    id: str
    role: str
    content: str
    timestamp: datetime
    metadata: Optional[Dict[str, Any]] = {}


def _get_transcript_text(session: DBRecordingSession) -> str:
    """Extract the best available transcript text from a recording session.

    v3.34.0: prefer the speaker-ATTRIBUTED transcript ("Name: utterance"
    lines from the diarized segments, raw codes normalized to "Speaker N")
    over the flat transcript_simple — the chat panel was answering
    "who said X" by guessing from unattributed text, the same bug class
    v3.33.0 fixed for summaries.
    """
    if session.transcript_diarized and isinstance(session.transcript_diarized, dict):
        # v3.44: render speaker names LIVE from the current profiles so chat
        # attribution reflects a rename without a transcript rewrite.
        from services.speaker_service import hydrate_diarized_for_session
        hydrated = hydrate_diarized_for_session(session)
        segments = (hydrated or {}).get("segments", []) if isinstance(hydrated, dict) else []
        if segments:
            from services.speaker_labels import build_attributed_transcript
            text, _speakers = build_attributed_transcript(segments)
            if text.strip():
                return text

    # Fall back to the flat transcript text
    if session.transcript_simple:
        return session.transcript_simple

    # Try raw transcript field (may be JSON)
    if session.transcript:
        try:
            data = json.loads(session.transcript) if isinstance(session.transcript, str) else session.transcript
            if isinstance(data, dict):
                if "segments" in data:
                    texts = [s.get("text", "") for s in data["segments"] if s.get("text")]
                    return " ".join(texts)
                if "text" in data:
                    return data["text"]
            return str(data)
        except (json.JSONDecodeError, TypeError):
            return str(session.transcript)

    return ""


def _get_summary_text(session: DBRecordingSession) -> str:
    """Extract summary text if available."""
    if session.final_summary and isinstance(session.final_summary, dict):
        parts = []
        if session.final_summary.get("executive"):
            parts.append(f"Executive Summary: {session.final_summary['executive']}")
        if session.final_summary.get("bullets"):
            parts.append("Key Points: " + "; ".join(session.final_summary["bullets"]))
        if session.final_summary.get("decisions"):
            parts.append("Decisions: " + "; ".join(session.final_summary["decisions"]))
        return "\n".join(parts)

    if session.summary:
        try:
            data = json.loads(session.summary) if isinstance(session.summary, str) else session.summary
            if isinstance(data, dict):
                parts = []
                if data.get("executive_summary") or data.get("executive"):
                    parts.append(data.get("executive_summary") or data.get("executive", ""))
                return "\n".join(parts) if parts else str(data)
            return str(data)
        except (json.JSONDecodeError, TypeError):
            return str(session.summary)

    return ""


def _build_system_prompt(session: DBRecordingSession, transcript: str, summary: str) -> str:
    """Build the system prompt with meeting context."""
    meeting_name = session.title or session.name or "Untitled Meeting"
    duration_str = f"{int(session.duration // 60)}m {int(session.duration % 60)}s" if session.duration else "unknown"
    date_str = session.created_at.strftime("%Y-%m-%d %H:%M") if session.created_at else "unknown"

    prompt = f"""You are an AI assistant for Meeting-Ops, a meeting recording and transcription system. You are helping the user understand and analyze a specific meeting recording.

Meeting: {meeting_name}
Date: {date_str}
Duration: {duration_str}
"""

    if summary:
        prompt += f"\nMeeting Summary:\n{summary}\n"

    # Include transcript, truncated if too long (keep under ~3000 tokens worth)
    if transcript:
        max_chars = 24000
        if len(transcript) > max_chars:
            prompt += f"\nMeeting Transcript (truncated to first {max_chars} characters):\n{transcript[:max_chars]}...\n"
        else:
            prompt += f"\nMeeting Transcript:\n{transcript}\n"

    prompt += """
Answer the user's questions about this meeting based on the transcript and summary above. Be concise, specific, and helpful. If the transcript doesn't contain enough information to answer a question, say so honestly. Do not make up information that isn't in the transcript."""

    return prompt


# API Endpoints

@router.post("/sessions/{session_id}/messages", response_model=AIResponse)
async def send_chat_message(
    session_id: str,
    request: SendMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Send a message about a meeting and get an AI response using the real LLM."""

    # v3.23.0 tier gate: per-session AI chat over the corpus is a TEXT
    # operation (read transcript text, run the server LLM with it as
    # context). Basic, Pro, Suite, Enterprise all get this — Free is
    # still browser-only. Was canonical_reprocess until v3.22.x; switched
    # so Basic users (text tier) can ask questions about their meetings.
    gate_feature_for_caller(current_user, "ai_chat_over_corpus", active_org)

    # Look up the recording session (canonical resolver: active-org first,
    # then the cross-org has_session_access fallback).
    from api.session_permissions import resolve_session_for_user

    session = resolve_session_for_user(
        db, active_org.organization.id, session_id, current_user
    )
    if not session:
        raise HTTPException(status_code=404, detail="Recording session not found")

    # Load transcript and summary
    transcript = _get_transcript_text(session)
    summary = _get_summary_text(session)

    if not transcript and not summary:
        return AIResponse(
            content="This meeting doesn't have a transcript or summary yet. Please ensure the recording has been processed before asking questions about it.",
            sources=[],
            metadata={"has_transcript": False, "has_summary": False}
        )

    # Build system prompt with meeting context
    system_prompt = _build_system_prompt(session, transcript, summary)

    # Save user message to DB
    _save_message(
        db,
        session_key=session_id,
        organization_id=active_org.organization.id,
        role="user",
        content=request.message,
    )

    # Load recent chat history from DB for conversational context
    history = _load_history(
        db,
        session_key=session_id,
        organization_id=active_org.organization.id,
        limit=20,
    )

    # Build user prompt including recent chat history
    if len(history) > 1:
        recent = history[-6:]  # Last 3 exchanges (user + assistant)
        history_text = "\n".join([
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in recent[:-1]  # Exclude current message
        ])
        user_prompt = f"Previous conversation:\n{history_text}\n\nUser: {request.message}"
    else:
        user_prompt = request.message

    # Call the LLM
    try:
        import time
        start_time = time.time()

        response_text = _llm_call(
            db,
            active_org.organization.id,
            task="chat",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=800,
            temperature=0.6,
        )

        inference_time = time.time() - start_time

        if not response_text:
            response_text = "I'm sorry, the AI model is currently unavailable. Please ensure the LLM service is running and try again."

        # Save assistant response to DB
        _save_message(
            db,
            session_key=session_id,
            organization_id=active_org.organization.id,
            role="assistant",
            content=response_text,
        )

        # Reload history length for metadata
        history = _load_history(
            db,
            session_key=session_id,
            organization_id=active_org.organization.id,
            limit=20,
        )

        logger.info(f"AI chat response generated for session {session_id} in {inference_time:.2f}s")

        return AIResponse(
            content=response_text,
            sources=[{
                "type": "meeting",
                "id": session_id,
                "title": session.title or session.name or "Meeting",
                "has_transcript": bool(transcript),
                "has_summary": bool(summary)
            }],
            metadata={
                "response_time_ms": int(inference_time * 1000),
                "transcript_length": len(transcript),
                "history_length": len(history)
            }
        )

    except Exception as e:
        logger.error(f"Error generating AI response: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to generate AI response: {str(e)}")


@router.get("/sessions/{session_id}/messages", response_model=List[ChatMessage])
async def get_chat_messages(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Get chat history for a specific recording session (from PostgreSQL)."""
    history = _load_history(
        db,
        session_key=session_id,
        organization_id=active_org.organization.id,
        limit=50,
    )

    messages = []
    for i, msg in enumerate(history):
        messages.append(ChatMessage(
            id=f"msg-{session_id}-{i}",
            role=msg["role"],
            content=msg["content"],
            timestamp=datetime.fromisoformat(msg["timestamp"]) if isinstance(msg["timestamp"], str) else msg["timestamp"],
            metadata=msg.get("metadata", {})
        ))

    return messages


@router.delete("/sessions/{session_id}/messages")
async def delete_chat_history(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Clear chat history for a specific recording session."""
    _clear_history(db, session_key=session_id, organization_id=active_org.organization.id)
    return {"message": "Chat history cleared"}


# ============================================================
# Cross-Meeting RAG Chat
# ============================================================

# Session key for cross-meeting RAG chat history in DB
RAG_SESSION_KEY = "__rag__"

# Max context budget in characters (~4000 tokens * 3 chars/token)
RAG_CONTEXT_MAX_CHARS = 24000


class RAGQueryRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    limit: int = Field(default=5, ge=1, le=20)


class RAGSource(BaseModel):
    session_id: str
    title: str
    score: float
    content_type: str = ""
    # Source traceability (data-flow rule: every generated answer must expose
    # which meeting/speaker/moment it came from, not just a bare title).
    speakers: List[str] = []
    created_at: str = ""
    source_kind: str = "meeting"


class RAGResponse(BaseModel):
    answer: str
    sources: List[RAGSource]
    metadata: Dict[str, Any] = {}


def _format_history_for_prompt(history: List[Dict[str, Any]], max_chars: int = 3000) -> str:
    """Format chat history for inclusion in LLM prompts.
    Truncates older messages first if total exceeds max_chars."""
    lines = []
    total = 0
    # Process from newest to oldest, then reverse
    for msg in reversed(history):
        role_label = "User" if msg["role"] == "user" else "Assistant"
        content = msg["content"][:600]  # Cap individual messages
        line = f"{role_label}: {content}"
        if total + len(line) > max_chars:
            break
        lines.insert(0, line)
        total += len(line) + 1  # +1 for newline
    return "\n".join(lines)


def _rewrite_query_with_history(
    current_query: str,
    history: List[Dict[str, Any]],
    db: Session,
    org_id: Optional[int],
) -> Optional[str]:
    """Use LLM to rewrite a follow-up question as a standalone search query.
    Returns the rewritten query, or None if rewriting fails or is unnecessary."""
    history_text = _format_history_for_prompt(history, max_chars=800)
    if not history_text:
        return None

    rewrite_prompt = (
        f"Given this conversation history:\n{history_text}\n\n"
        f'The user\'s follow-up question is: "{current_query}"\n\n'
        "Rewrite this as a standalone search query that captures the full intent, "
        "resolving any pronouns or references from the conversation. "
        "Output ONLY the rewritten query, nothing else."
    )

    try:
        rewritten = _llm_call(
            db,
            org_id,
            task="fast",
            system_prompt=(
                "You rewrite follow-up questions into standalone search queries. "
                "Output only the rewritten query with no explanation or preamble."
            ),
            user_prompt=rewrite_prompt,
            max_tokens=100,
            temperature=0.1,
        )
        if rewritten and len(rewritten.strip()) > 3:
            # Strip quotes the LLM might wrap the query in
            result = rewritten.strip().strip('"').strip("'")
            return result
    except Exception as e:
        logger.warning(f"Query rewrite failed, using original query: {e}")

    return None


@router.post("/rag/query", response_model=RAGResponse)
async def rag_query(
    request: RAGQueryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """
    Cross-meeting RAG query with multi-turn conversation support.
    Searches Qdrant for relevant chunks across ALL meetings, assembles context,
    and sends to Granite 3.3 2B for an answer citing which meeting each fact
    comes from. Uses chat history for query rewriting and conversational context.
    """
    # v3.23.0 tier gate: cross-meeting RAG is a TEXT-search-plus-LLM op
    # over the user's existing server-stored transcript corpus. Basic
    # tier ships with this enabled. Free is browser-only.
    gate_feature_for_caller(current_user, "cross_meeting_search", active_org)

    import time

    try:
        from services.semantic_search_service import semantic_search
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Semantic search service is not available. Ensure Qdrant is running."
        )

    # 1. Load recent chat history for conversational context
    rag_history = _load_history(
        db,
        session_key=RAG_SESSION_KEY,
        organization_id=active_org.organization.id,
        limit=6,
    )

    # 2. Rewrite query using history context (if there IS history)
    search_query = request.message
    rewritten_query = None
    if rag_history:
        rewritten = _rewrite_query_with_history(
            request.message,
            rag_history,
            db,
            active_org.organization.id,
        )
        if rewritten and rewritten.lower() != request.message.lower():
            rewritten_query = rewritten
            search_query = rewritten
            logger.info(
                f"RAG query rewritten: '{request.message}' -> '{search_query}'"
            )

    # 3. Search for relevant chunks using (possibly rewritten) query
    try:
        chunks = semantic_search.search_chunks(
            query=search_query,
            limit=request.limit * 3,  # Over-fetch, then trim to context budget
            organization_id=active_org.organization.id,
        )
    except Exception as e:
        logger.error(f"RAG search failed: {e}")
        raise HTTPException(status_code=503, detail=f"Semantic search failed: {str(e)}")

    if not chunks:
        # Still save to history even if no results
        _save_message(
            db,
            session_key=RAG_SESSION_KEY,
            organization_id=active_org.organization.id,
            role="user",
            content=request.message,
        )
        no_results_answer = (
            "No indexed meetings found matching your query. "
            "Please ensure meetings have been recorded and indexed in the semantic search system."
        )
        _save_message(
            db,
            session_key=RAG_SESSION_KEY,
            organization_id=active_org.organization.id,
            role="assistant",
            content=no_results_answer,
        )
        return RAGResponse(
            answer=no_results_answer,
            sources=[],
            metadata={
                "chunks_found": 0,
                "rewritten_query": rewritten_query,
            }
        )

    # 3b. Rerank the over-fetched candidates with Infinity's reranker so the
    # most QUERY-RELEVANT chunks (not merely the most embedding-similar) lead
    # the context. Best-effort + suite-consistent (same reranker the other UC
    # apps use); if Infinity rerank is unreachable we keep the RRF order.
    try:
        from services.providers.impl_reranking import InfinityRerankingProvider

        rr_endpoint = (
            os.getenv("INFINITY_RERANKER_ENDPOINT")
            or os.getenv("INFINITY_ENDPOINT")
            or ""
        )
        if rr_endpoint and len(chunks) > 1:
            reranker = InfinityRerankingProvider(
                api_key=os.getenv("INFINITY_API_KEY", ""),
                endpoint=rr_endpoint,
                model=os.getenv("RERANKER_MODEL", "Qwen/Qwen3-Reranker-0.6B"),
            )
            results = await reranker.rerank(
                query=search_query,
                documents=[c.get("text", "") for c in chunks],
                top_n=min(request.limit, len(chunks)),
            )
            ordered = []
            for r in results or []:
                idx = r.get("index")
                if isinstance(idx, int) and 0 <= idx < len(chunks):
                    c = dict(chunks[idx])
                    c["rerank_score"] = r.get("relevance_score", r.get("score"))
                    ordered.append(c)
            if ordered:
                logger.info(
                    "RAG reranked %d candidates -> top %d via Infinity",
                    len(chunks), len(ordered),
                )
                chunks = ordered
    except Exception as e:  # noqa: BLE001
        logger.warning(f"RAG rerank skipped (non-fatal): {e}")

    # 4. Assemble context within token budget (reduced from 12000 to 9000 to
    #    make room for ~1500 chars of chat history in the prompt)
    context_budget = 18000 if rag_history else RAG_CONTEXT_MAX_CHARS
    context_parts = []
    total_chars = 0
    seen_sources = {}  # session_id -> best RAGSource

    for chunk in chunks:
        # Format chunk with meeting metadata
        title = chunk.get("title", "Untitled")
        session_id = chunk.get("session_id", "")
        content_type = chunk.get("content_type", "transcript")
        text = chunk.get("text", "")
        speakers = chunk.get("speakers", [])
        created_at = chunk.get("created_at", "")
        score = chunk.get("score", 0.0)

        # Build context block
        source_kind = chunk.get("source_kind", "meeting")
        source_label = {"meeting": "Meeting", "lecture": "Lecture", "document": "Document"}.get(source_kind, "Meeting")
        header = f"[{source_label}: {title}"
        if created_at:
            header += f" | Date: {created_at[:10]}"
        if speakers:
            header += f" | Speakers: {', '.join(speakers)}"
        header += f" | Type: {content_type}]"

        block = f"{header}\n{text}\n"

        if total_chars + len(block) > context_budget:
            # Try to fit a truncated version
            remaining = context_budget - total_chars
            if remaining > 200:  # Only include if we can fit something useful
                block = f"{header}\n{text[:remaining - len(header) - 10]}...\n"
                context_parts.append(block)
                total_chars += len(block)
            break

        context_parts.append(block)
        total_chars += len(block)

        # Track source meetings
        if session_id not in seen_sources or score > seen_sources[session_id].score:
            seen_sources[session_id] = RAGSource(
                session_id=session_id,
                title=title,
                score=score,
                content_type=content_type,
                speakers=speakers if isinstance(speakers, list) else [],
                created_at=created_at or "",
                source_kind=source_kind,
            )

    context_text = "\n".join(context_parts)

    # 5. Build system prompt with context AND conversation history
    system_prompt = (
        "You are a meeting analyst for Meeting-Ops. Answer the user's question "
        "using ONLY the meeting excerpts provided below. Cite which meeting each "
        "fact comes from by referencing its title. If the excerpts do not contain "
        "enough information to answer, say so honestly.\n\n"
        "Meeting Excerpts:\n"
        f"{context_text}"
    )

    if rag_history:
        history_text = _format_history_for_prompt(rag_history)
        system_prompt += (
            f"\n\nPrevious conversation for context:\n{history_text}"
        )

    # 6. Call LLM
    start_time = time.time()

    try:
        answer = _llm_call(
            db,
            active_org.organization.id,
            task="chat",
            system_prompt=system_prompt,
            user_prompt=request.message,
            max_tokens=800,
            temperature=0.3,  # Lower temperature for factual RAG answers
        )
    except Exception as e:
        logger.error(f"RAG LLM call failed: {e}")
        raise HTTPException(status_code=503, detail=f"LLM inference failed: {str(e)}")

    inference_time = time.time() - start_time

    if not answer:
        answer = "The AI model is currently unavailable. Please ensure the LLM service is running."

    # 7. Persist to DB
    _save_message(
        db,
        session_key=RAG_SESSION_KEY,
        organization_id=active_org.organization.id,
        role="user",
        content=request.message,
    )
    _save_message(
        db,
        session_key=RAG_SESSION_KEY,
        organization_id=active_org.organization.id,
        role="assistant",
        content=answer,
    )

    # 8. Build response
    sources = sorted(seen_sources.values(), key=lambda s: s.score, reverse=True)

    logger.info(
        f"RAG query answered in {inference_time:.2f}s, "
        f"{len(chunks)} chunks searched, {len(sources)} source meetings"
        f"{f', query rewritten to: {search_query}' if rewritten_query else ''}"
    )

    return RAGResponse(
        answer=answer,
        sources=sources,
        metadata={
            "response_time_ms": int(inference_time * 1000),
            "chunks_retrieved": len(chunks),
            "context_chars": total_chars,
            "source_meetings": len(sources),
            "rewritten_query": rewritten_query,
            "history_length": len(rag_history),
        }
    )


@router.get("/rag/history")
async def get_rag_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Get the cross-meeting RAG chat history (from PostgreSQL)."""
    history = _load_history(
        db,
        session_key=RAG_SESSION_KEY,
        organization_id=active_org.organization.id,
        limit=50,
    )
    messages = []
    for i, msg in enumerate(history):
        messages.append({
            "id": f"rag-msg-{i}",
            "role": msg["role"],
            "content": msg["content"],
        })
    return messages


@router.delete("/rag/history")
async def clear_rag_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Clear cross-meeting RAG chat history."""
    _clear_history(db, session_key=RAG_SESSION_KEY, organization_id=active_org.organization.id)
    return {"message": "RAG chat history cleared"}
