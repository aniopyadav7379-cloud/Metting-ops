"""
Local agent source — Meeting-Ops native agents (currently just meeting-rag).
"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from .base import AgentDescriptor, AgentSource


LOCAL_AGENT_SPECS = [
    {
        "id": "meeting-rag",
        "name": "Meeting Assistant",
        "description": (
            "Tool-using assistant for your meetings. Searches, fetches "
            "transcripts and summaries, and answers questions grounded in "
            "real session data."
        ),
        "source": "local",
        "domain": "meetings",
        "capabilities": [
            "tool-use",
            "search",
            "rag",
            "citations",
            "scope-filters",
            "analytics",
        ],
        "icon": "🎙️",
        "streaming": True,
    },
]


class LocalAgentSource(AgentSource):
    name = "local"

    async def list_agents(
        self,
        request: Request,
        db: Session,
        org_id: int,
    ):
        return [AgentDescriptor(**spec) for spec in LOCAL_AGENT_SPECS]

    async def get_agent(
        self,
        agent_id: str,
        request: Request,
        db: Session,
        org_id: int,
    ):
        for spec in LOCAL_AGENT_SPECS:
            if spec["id"] == agent_id:
                return AgentDescriptor(**spec)
        return None
