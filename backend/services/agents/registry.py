"""
AgentRegistry — federate local + external (Brigade, future others) agents.

The registry holds a list of AgentSource objects and aggregates their
list_agents() output into a single AgentDescriptor list per request.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from .base import AgentDescriptor, AgentSource
from .brigade import BrigadeAgentSource
from .local import LocalAgentSource

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Aggregates local + external agent sources."""

    def __init__(self, sources: list[AgentSource]):
        self._sources = sources

    async def list_agents(
        self,
        request: Request,
        db: Session,
        org_id: int,
    ) -> tuple[list[AgentDescriptor], list[str]]:
        """Returns (agents, warnings). Sources that fail produce a warning
        but do not raise."""
        all_agents: list[AgentDescriptor] = []
        warnings: list[str] = []
        for source in self._sources:
            try:
                agents = await source.list_agents(request, db, org_id)
                all_agents.extend(agents)
            except Exception as e:  # noqa: BLE001 — degrade gracefully
                msg = f"Source '{source.name}' failed: {e}"
                logger.warning(msg)
                warnings.append(msg)
        return all_agents, warnings

    async def get_agent(
        self,
        agent_id: str,
        request: Request,
        db: Session,
        org_id: int,
    ) -> Optional[AgentDescriptor]:
        for source in self._sources:
            try:
                agent = await source.get_agent(agent_id, request, db, org_id)
                if agent:
                    return agent
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Source '{source.name}' get_agent failed: {e}")
        return None


_registry: Optional[AgentRegistry] = None


def get_agent_registry() -> AgentRegistry:
    """Process-wide registry. Sources are stateless — request-scoped data
    flows in via list_agents()/get_agent() arguments."""
    global _registry
    if _registry is None:
        _registry = AgentRegistry(
            sources=[
                LocalAgentSource(),
                BrigadeAgentSource(),
            ]
        )
    return _registry
