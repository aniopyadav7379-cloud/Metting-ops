"""
Multi-agent dispatcher API.

Endpoints:
  GET  /api/agents/available     — merged list of local + Brigade agents
  POST /api/agents/chat          — dispatcher; routes to RAG or Brigade
  GET  /api/agents/{agent_id}    — agent metadata (local descriptor or proxy)

Bearer auth comes from oauth2-proxy and is forwarded verbatim to Brigade.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from services.agents import (
    brigade_agent_real_id,
    get_agent_registry,
)
from services.agents.brigade import _bearer_from_request, _brigade_base

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["Agents"])


class AvailableAgentsResponse(BaseModel):
    agents: list[dict[str, Any]]
    warnings: list[str] = Field(default_factory=list)


class AgentMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    agent_id: str
    messages: list[AgentMessage] = Field(default_factory=list)
    stream: Optional[bool] = True
    scope: Optional[dict[str, Any]] = None


@router.get("/available", response_model=AvailableAgentsResponse)
async def list_available_agents(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Merged list of local + federated agents available to this user."""
    registry = get_agent_registry()
    agents, warnings = await registry.list_agents(request, db, active_org.organization.id)
    return AvailableAgentsResponse(
        agents=[a.to_dict() for a in agents],
        warnings=warnings,
    )


@router.get("/info/{agent_id:path}")
async def get_agent_metadata(
    agent_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Return descriptor metadata for an agent.

    Note: registered under `/info/<id>` rather than `/{id}` to avoid colliding
    with the existing agent_management router that owns `/api/agents/{slug}`.
    """
    registry = get_agent_registry()
    agent = await registry.get_agent(agent_id, request, db, active_org.organization.id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return agent.to_dict()


def _last_user_message(messages: list[AgentMessage]) -> str:
    for m in reversed(messages):
        if m.role == "user":
            return m.content
    raise HTTPException(status_code=400, detail="No user message in messages[]")


def _history_excluding_current(messages: list[AgentMessage]) -> list[dict]:
    """Everything but the trailing user turn (the 'current' message)."""
    if not messages:
        return []
    out = [m.dict() for m in messages]
    # Walk from end and drop the last user message we treat as current.
    for i in range(len(out) - 1, -1, -1):
        if out[i].get("role") == "user":
            return out[:i]
    return out


async def _proxy_brigade_stream(
    real_agent_id: str,
    user_message: str,
    history: list[dict],
    bearer: str,
) -> StreamingResponse:
    """Open Brigade's SSE chat stream and re-emit it in our SSE schema.

    Brigade emits events like:
      data: {"type": "start", "agent": "...", "model": "...", "tools": [...]}
      data: {"type": "token", "content": "..."}
      data: {"type": "complete", "content": "...", "model": "...", "toolCalls": [...]}
      data: [DONE]

    We translate to our wire format used by the frontend chat panel:
      data: {"token": "..."}
      data: {"done": true, "sources": [], "metadata": {...}}
    """
    url = f"{_brigade_base()}/api/v1/agents/{real_agent_id}/chat"

    body = {
        "message": user_message,
        "stream": True,
        "history": [
            {"role": h.get("role", "user"), "content": str(h.get("content", ""))}
            for h in (history or [])
        ],
    }

    async def event_stream():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers={
                        "Authorization": bearer,
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    json=body,
                ) as resp:
                    if resp.status_code != 200:
                        text_bytes = await resp.aread()
                        body_snippet = text_bytes.decode(errors="ignore")[:200]
                        err_msg = f"Brigade returned HTTP {resp.status_code}: {body_snippet}"
                        yield f"data: {json.dumps({'error': err_msg})}\n\n"
                        done_payload = {
                            "done": True,
                            "sources": [],
                            "metadata": {"source": "brigade", "agent_id": real_agent_id},
                        }
                        yield f"data: {json.dumps(done_payload)}\n\n"
                        return

                    metadata: dict[str, Any] = {"source": "brigade", "agent_id": real_agent_id}
                    full_content = ""

                    async for raw_line in resp.aiter_lines():
                        line = raw_line.strip()
                        if not line:
                            continue
                        if not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if payload == "[DONE]":
                            continue
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        evt_type = evt.get("type")
                        if evt_type == "token":
                            tok = evt.get("content") or ""
                            if tok:
                                full_content += tok
                                yield f"data: {json.dumps({'token': tok})}\n\n"
                        elif evt_type == "start":
                            for k in ("agent", "model", "tools"):
                                if k in evt:
                                    metadata[k] = evt[k]
                        elif evt_type == "complete":
                            for k in ("model", "toolCalls", "tokens"):
                                if k in evt:
                                    metadata[k] = evt[k]
                            # If we never got token events, surface the final content.
                            if not full_content and evt.get("content"):
                                final = evt["content"]
                                yield f"data: {json.dumps({'token': final})}\n\n"
                        elif evt_type == "error":
                            yield f"data: {json.dumps({'error': evt.get('message') or evt.get('error') or 'Brigade error'})}\n\n"

                    yield f"data: {json.dumps({'done': True, 'sources': [], 'metadata': metadata})}\n\n"
        except httpx.HTTPError as e:
            logger.error(f"Brigade stream error: {e}")
            yield f"data: {json.dumps({'error': f'Brigade unreachable: {e}'})}\n\n"
            yield f"data: {json.dumps({'done': True, 'sources': [], 'metadata': {'source': 'brigade', 'error': True}})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _brigade_sync_chat(
    real_agent_id: str,
    user_message: str,
    history: list[dict],
    bearer: str,
) -> dict[str, Any]:
    url = f"{_brigade_base()}/api/v1/agents/{real_agent_id}/chat"
    body = {
        "message": user_message,
        "stream": False,
        "history": [
            {"role": h.get("role", "user"), "content": str(h.get("content", ""))}
            for h in (history or [])
        ],
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
        resp = await client.post(
            url,
            headers={"Authorization": bearer, "Content-Type": "application/json"},
            json=body,
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Brigade chat failed: {resp.text[:300]}",
        )
    return resp.json()


@router.post("/chat")
async def dispatch_chat(
    body: ChatRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Dispatcher — route the message to the chosen agent.

    Local agents are handled in-process. Brigade agents are proxied via
    SSE (or sync if stream=False).
    """
    org_id = active_org.organization.id
    agent_id = body.agent_id.strip()
    stream = bool(body.stream) if body.stream is not None else True
    user_message = _last_user_message(body.messages)
    history = _history_excluding_current(body.messages)

    if agent_id == "meeting-rag":
        # Tool-using agent — server-side loop on Qwen 3.6 with the helpers
        # in services/agent_tools. The federated dispatcher routes through
        # the tool-use loop so the Dashboard ask bar can call tools
        # (search, details, transcripts, analytics) instead of just
        # hitting Qdrant once.
        from services.agents.meeting_rag import run_meeting_rag
        return await run_meeting_rag(
            user_message=user_message,
            history=history,
            db=db,
            org_id=org_id,
            current_user=current_user,
            scope=body.scope,
        )

    real_id = brigade_agent_real_id(agent_id)
    if real_id is not None:
        bearer = _bearer_from_request(request)
        if not bearer:
            raise HTTPException(
                status_code=401,
                detail="Brigade requires a bearer JWT; none on request",
            )

        if stream:
            return await _proxy_brigade_stream(real_id, user_message, history, bearer)

        result = await _brigade_sync_chat(real_id, user_message, history, bearer)
        # Wrap as SSE-style envelope so frontend can use a uniform reader.
        async def sync_stream():
            content = result.get("content") or ""
            if content:
                yield f"data: {json.dumps({'token': content})}\n\n"
            metadata = {
                "source": "brigade",
                "agent_id": real_id,
                "model": result.get("model"),
                "provider": result.get("provider"),
                "usage": result.get("usage"),
            }
            yield f"data: {json.dumps({'done': True, 'sources': [], 'metadata': metadata})}\n\n"
        return StreamingResponse(sync_stream(), media_type="text/event-stream")

    raise HTTPException(status_code=404, detail=f"Unknown agent_id '{agent_id}'")
