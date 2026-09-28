"""Base types for the agent registry — kept in a separate module so that
LocalAgentSource and BrigadeAgentSource can import without circulars."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Optional

from fastapi import Request
from sqlalchemy.orm import Session


@dataclass
class AgentDescriptor:
    """Uniform descriptor surfaced to the frontend agent picker."""
    id: str
    name: str
    description: str
    source: str  # 'local' | 'brigade' | future others
    domain: Optional[str] = None
    capabilities: Optional[list[str]] = None
    requires_role: Optional[str] = None
    icon: Optional[str] = None
    streaming: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentSource:
    """Abstract base — sources contribute agents to the registry."""

    name: str = "abstract"

    async def list_agents(
        self,
        request: Request,
        db: Session,
        org_id: int,
    ) -> list[AgentDescriptor]:
        raise NotImplementedError

    async def get_agent(
        self,
        agent_id: str,
        request: Request,
        db: Session,
        org_id: int,
    ) -> Optional[AgentDescriptor]:
        agents = await self.list_agents(request, db, org_id)
        for a in agents:
            if a.id == agent_id:
                return a
        return None
