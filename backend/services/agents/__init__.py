"""
Multi-source agent federation for Meeting-Ops.

Aggregates local Meeting-Ops agents (e.g. meeting-rag) with agents from
external orchestrators (currently Brigade) and exposes a uniform
AgentDescriptor + AgentRegistry contract to the API layer.
"""

from .base import AgentDescriptor, AgentSource
from .brigade import BrigadeAgentSource, brigade_agent_real_id
from .local import LocalAgentSource
from .registry import AgentRegistry, get_agent_registry

__all__ = [
    "AgentDescriptor",
    "AgentRegistry",
    "AgentSource",
    "LocalAgentSource",
    "BrigadeAgentSource",
    "get_agent_registry",
    "brigade_agent_real_id",
]
