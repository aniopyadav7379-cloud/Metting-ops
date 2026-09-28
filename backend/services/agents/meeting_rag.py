"""
Meeting-RAG agent — tool-using assistant that streams SSE.

Wraps the org-aware LLM provider with an OpenAI-style tool-use loop. The
loop calls the helpers in ``services/agent_tools`` until the model returns
a final assistant message, then streams that final message back to the
client token-by-token. Tool calls are surfaced as their own SSE events so
the UI can show "calling search_meetings…" indicators if it wants.

Wire format (one JSON object per SSE ``data:`` line, matching what
``frontend/src/components/dashboard/AskBar.tsx`` already consumes):

    {"event": "tool_call", "name": "...", "arguments": {...}}
    {"event": "tool_result", "name": "...", "preview": "..."}
    {"token": "..."}
    {"done": true, "sources": [...], "metadata": {...}}
    {"error": "..."}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncGenerator, Optional

import httpx
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from auth.models import User
from services.agent_tools import (
    MEETING_RAG_TOOLS,
    execute_tool,
    load_recent_history,
    persist_turn,
)
from services.providers.impl_llm import apply_enable_thinking
from services.providers.registry import get_provider_registry

logger = logging.getLogger(__name__)

_MAX_TOOL_ITERATIONS = 6
_TOOL_RESULT_PREVIEW_CHARS = 400
_HTTP_TIMEOUT = httpx.Timeout(480.0, connect=10.0)

SYSTEM_PROMPT = (
    "You are the Meeting Assistant for UC-Meeting-Ops. Help the user "
    "understand their recorded meetings: search them, fetch details, read "
    "transcripts, summarize, and answer follow-up questions. "
    "\n\n"
    "Always ground your answers in the actual data returned by your tools. "
    "Never invent meeting titles, attendees, quotes, dates, or decisions. "
    "If a tool returns no results, say so honestly. "
    "\n\n"
    "Standard workflow when the user asks about a topic:\n"
    "  1. Call search_meetings with the most salient keyword(s).\n"
    "  2. If it returns one obvious match, call chat_with_meeting on that "
    "     session_id with the user's actual question — that loads the full "
    "     transcript and produces a grounded answer.\n"
    "  3. If it returns several matches, either call chat_with_meeting on "
    "     each (or the top 2) or call ask_about_meetings to synthesize.\n"
    "  4. Do NOT call ask_about_meetings AS A FOLLOW-UP to search_meetings "
    "     using the same broad phrase — ask_about_meetings runs its own "
    "     search and you'll get the same hits back. Drill into a specific "
    "     session_id instead.\n"
    "\n"
    "Other tools:\n"
    "- get_meeting_details — structured summary (decisions, action items) for one session.\n"
    "- get_meeting_transcript — raw transcript text when you need quotes.\n"
    "- list_meetings — 'show me my recent meetings'.\n"
    "- get_analytics — counts, durations, top speakers over a window.\n"
    "- get_meeting_insights — cached sentiment/keywords/engagement metrics.\n"
    "\n"
    "Write tools:\n"
    "- propose_create_session, propose_rename_session, propose_add_tag, propose_remove_tag — return a confirmation proposal; do not claim the change has already happened.\n"
    "- propose_trigger_reprocess — return a confirmation proposal; do not claim reprocessing has started until confirmed.\n"
    "- propose_draft_followup_email — return a confirmation proposal and draft text; wait for confirmation before presenting it as the final draft.\n"
    "If a write tool returns a confirmation proposal, surface the proposal and stop the loop instead of pretending the mutation was applied.\n"
    "\n"
    "Cite meetings by title (and date when relevant). Be concise. If a "
    "search snippet already contains the answer, you can quote from it "
    "directly without a follow-up tool call."
)


def _llm_endpoint_and_headers(provider: Any) -> tuple[str, dict[str, str], str]:
    """Pull endpoint, auth headers, and model name off a LiteLLMProvider.

    The provider exposes these as plain attributes; we hit ``/chat/completions``
    directly so we can pass ``tools``+``tool_choice`` which the provider's
    typed ``chat_*`` methods don't expose."""
    endpoint = getattr(provider, "endpoint", "").rstrip("/")
    api_key = getattr(provider, "api_key", "") or ""
    model = getattr(provider, "model", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return endpoint, headers, model


async def _call_llm_with_tools(
    provider: Any,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]],
    enable_thinking: bool = False,
) -> dict[str, Any]:
    """Single non-streamed chat.completions call carrying tools. Returns the
    raw assistant message (``{role, content, tool_calls?}``)."""
    endpoint, headers, model = _llm_endpoint_and_headers(provider)
    payload = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "temperature": 0.3,
        "max_tokens": 1024,
        "stream": False,
    }
    # Only stamps enable_thinking for a local Qwen target (gated by
    # MEETING_OPS_DISABLE_THINKING); left off external/frontier endpoints.
    apply_enable_thinking(payload, model, endpoint, enable_thinking)

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(
            f"{endpoint}/chat/completions",
            headers=headers,
            json=payload,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"LLM returned HTTP {resp.status_code}: {resp.text[:240]}"
            )
        data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("LLM returned no choices")
    return choices[0].get("message") or {}


async def _stream_llm_final(
    provider: Any,
    messages: list[dict[str, Any]],
) -> AsyncGenerator[str, None]:
    """Stream the *final* assistant turn token-by-token. No tools are passed
    here — the loop has already decided no more tool calls are needed."""
    endpoint, headers, model = _llm_endpoint_and_headers(provider)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 1024,
        "stream": True,
    }
    # Final-answer turn: never want the <think> trace. Gated to local Qwen.
    apply_enable_thinking(payload, model, endpoint, thinking=False)

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        async with client.stream(
            "POST",
            f"{endpoint}/chat/completions",
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", errors="ignore")[:240]
                raise RuntimeError(f"LLM stream HTTP {resp.status_code}: {body}")

            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                blob = line[len("data:"):].strip()
                if blob == "[DONE]":
                    break
                try:
                    chunk = json.loads(blob)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                token = delta.get("content")
                if token:
                    yield token


def _coerce_arguments(raw: Any) -> dict[str, Any]:
    """``tool_calls[].function.arguments`` is a JSON-encoded string per the
    OpenAI spec. Some llama.cpp builds return an object directly — accept
    both."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return {}


def _truncate_for_preview(value: Any) -> str:
    text = json.dumps(value, default=str)
    if len(text) <= _TOOL_RESULT_PREVIEW_CHARS:
        return text
    return text[:_TOOL_RESULT_PREVIEW_CHARS] + "…"


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _sources_from_history(
    tool_history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect the most useful 'sources' we can give the UI based on the
    tool calls the model made. We surface any meeting brief that was
    returned, deduped by session_id, with the most-recent first."""
    seen: dict[str, dict[str, Any]] = {}
    for entry in tool_history:
        result = entry.get("result")
        if not isinstance(result, dict):
            continue
        # search_meetings / list_meetings / ask_about_meetings shapes
        candidates: list[dict[str, Any]] = []
        if isinstance(result.get("results"), list):
            candidates.extend(result["results"])
        if isinstance(result.get("meetings"), list):
            candidates.extend(result["meetings"])
        if isinstance(result.get("sources"), list):
            candidates.extend(result["sources"])
        # Direct details / transcript / chat shapes carry session_id at top level.
        if result.get("session_id"):
            candidates.append(result)
        for c in candidates:
            sid = c.get("session_id")
            if not sid:
                continue
            seen.setdefault(str(sid), {
                "session_id": sid,
                "title": c.get("title"),
                "created_at": c.get("created_at"),
                "score": c.get("score"),
                # Carry the search snippet through so the UI can show context
                # under each source. None for tool results without one (list /
                # details); already truncated upstream, so no payload concern.
                "snippet": c.get("snippet"),
            })
    return list(seen.values())[:10]


async def run_meeting_rag(
    *,
    user_message: str,
    history: list[dict[str, Any]],
    db: Session,
    org_id: int,
    current_user: User,
    scope: Optional[dict[str, Any]] = None,
) -> StreamingResponse:
    """Entry point used by ``api/agents.py``.

    Builds the message list, runs the tool-use loop, then streams the final
    assistant turn over SSE in the wire format the AskBar already consumes.
    """
    registry = get_provider_registry(db)
    llm_provider = registry.get_llm(org_id, task="chat")

    persisted = load_recent_history(db, org_id, limit=10)
    request_history: list[dict[str, Any]] = []
    seen_pairs = set()
    for src in (persisted, history or []):
        for entry in src:
            role = entry.get("role")
            content = entry.get("content")
            if role not in ("user", "assistant") or not content:
                continue
            key = (role, content)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            request_history.append({"role": role, "content": content})
    # Keep the prompt budget sane — last 12 turns of context is plenty for a
    # tool-using assistant whose grounding comes from tool results, not chat
    # history.
    request_history = request_history[-12:]

    from services.graph_augmented_retrieval import resolve_graph_augmentation_enabled

    graph_augment = resolve_graph_augmentation_enabled(scope)

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(request_history)
    messages.append({"role": "user", "content": user_message})

    tool_history: list[dict[str, Any]] = []

    async def event_stream() -> AsyncGenerator[str, None]:
        nonlocal messages
        full_answer = ""
        t0 = time.time()
        pending_proposal: dict[str, Any] | None = None
        try:
            for iteration in range(_MAX_TOOL_ITERATIONS):
                assistant_msg = await _call_llm_with_tools(
                    llm_provider,
                    messages,
                    tools=MEETING_RAG_TOOLS,
                )
                tool_calls = assistant_msg.get("tool_calls") or []

                if not tool_calls:
                    # Final answer path — preserve any pre-roll content the
                    # non-streamed call already returned, then re-stream from
                    # the model for live tokens. Most builds give us empty
                    # content when there are no tool_calls and we'll fill it
                    # via the streaming call below.
                    pre_roll = (assistant_msg.get("content") or "").strip()
                    # Append the (possibly empty) assistant turn we just
                    # received so the streaming follow-up has the correct
                    # conversation tail. We strip tool_calls because there
                    # are none.
                    messages.append({"role": "assistant", "content": pre_roll})
                    try:
                        async for token in _stream_llm_final(llm_provider, messages[:-1]):
                            full_answer += token
                            yield _sse({"token": token})
                    except Exception as stream_exc:
                        logger.warning(
                            "Final stream failed, falling back to non-streamed text: %s",
                            stream_exc,
                        )
                        if pre_roll:
                            full_answer = pre_roll
                            yield _sse({"token": pre_roll})
                    break

                # Echo the assistant's tool-call turn into the conversation so
                # the next LLM call sees consistent state.
                messages.append({
                    "role": "assistant",
                    "content": assistant_msg.get("content") or "",
                    "tool_calls": tool_calls,
                })

                # Execute each tool call. We deliberately do these sequentially
                # — most are sub-second DB hits, and a few (chat_with_meeting /
                # ask_about_meetings) call the LLM and parallelism would hammer
                # the single P40 unhelpfully.
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = fn.get("name") or "<unnamed>"
                    args = _coerce_arguments(fn.get("arguments"))
                    yield _sse({
                        "event": "tool_call",
                        "name": name,
                        "arguments": args,
                    })

                    try:
                        result = await execute_tool(
                            name,
                            args,
                            db=db,
                            org_id=org_id,
                            current_user=current_user,
                            llm_provider=llm_provider,
                            graph_augment=graph_augment,
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = {"error": f"{name} crashed: {exc}"}
                        logger.exception("Tool %s crashed during dispatch", name)

                    tool_history.append({
                        "name": name,
                        "arguments": args,
                        "result": result,
                    })

                    yield _sse({
                        "event": "tool_result",
                        "name": name,
                        "preview": _truncate_for_preview(result),
                    })

                    if isinstance(result, dict) and result.get("status") == "needs_confirmation":
                        pending_proposal = result
                        full_answer = result.get("preview") or full_answer
                        yield _sse({
                            "event": "needs_confirmation",
                            "name": name,
                            "proposal": result,
                        })
                        messages.append({
                            "role": "assistant",
                            "content": result.get("preview") or "",
                        })
                        break

                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "name": name,
                        "content": json.dumps(result, default=str),
                    })

                if pending_proposal is not None:
                    break

            else:
                # Iteration cap hit — force a final answer. We strip tools so
                # the model has no choice but to respond in prose.
                logger.warning("meeting-rag hit max iterations (%s)", _MAX_TOOL_ITERATIONS)
                messages.append({
                    "role": "system",
                    "content": (
                        "You've used the maximum number of tool calls. "
                        "Provide your best final answer to the user using "
                        "the tool results above."
                    ),
                })
                async for token in _stream_llm_final(llm_provider, messages):
                    full_answer += token
                    yield _sse({"token": token})

        except Exception as exc:  # noqa: BLE001
            logger.exception("meeting-rag loop failed")
            err = f"Meeting assistant failed: {exc}"
            full_answer = full_answer or err
            yield _sse({"error": err})

        sources = _sources_from_history(tool_history)
        elapsed_ms = int((time.time() - t0) * 1000)
        metadata = {
            "agent_id": "meeting-rag",
            "tool_calls": [
                {"name": e["name"], "arguments": e["arguments"]}
                for e in tool_history
            ],
            "iterations": len(tool_history),
            "response_time_ms": elapsed_ms,
            "model": getattr(llm_provider, "model", None),
            "pending_confirmation": pending_proposal,
        }
        yield _sse({"done": True, "sources": sources, "metadata": metadata})

        # Persist the turn so subsequent dashboard asks see continuity.
        await asyncio.to_thread(
            persist_turn, db, org_id,
            role="user", content=user_message,
        )
        await asyncio.to_thread(
            persist_turn, db, org_id,
            role="assistant",
            content=full_answer or "",
            metadata={
                "tools_called": [e["name"] for e in tool_history],
                "sources": sources,
                "pending_confirmation": pending_proposal,
            },
        )

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def empty_question_response() -> StreamingResponse:
    """Standard SSE response when the request body has no user message."""
    async def gen() -> AsyncGenerator[str, None]:
        yield _sse({"error": "No user message provided."})
        yield _sse({"done": True, "sources": [], "metadata": {"agent_id": "meeting-rag"}})
    return StreamingResponse(gen(), media_type="text/event-stream")
