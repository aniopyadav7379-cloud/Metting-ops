"""Knowledge Graph — cross-meeting, person-centric subgraph.

Surfaces a Speaker's Brigade (FalkorDB) neighborhood as an in-app graph,
reusing the per-session shaper (`_shape_brigade_graph`) and the 3D viewer's
``{nodes, links, focus}`` wire contract.

Two enrichments over a raw Brigade fetch (both added 2026-06-07):
  * **Connected web.** Brigade's ``fetch_entity_context`` returns the focus
    node's DIRECT edges + a flat 2-hop entity list but NOT the edges among
    neighbors, so 2-hop nodes (a meeting's topics/decisions/co-speakers) float.
    For ``hops>=2`` we expand each Meeting neighbor in parallel and merge its
    edges, so the graph renders as a connected web instead of a star + cloud.
  * **Postgres hydration.** Brigade nodes carry only id + type — the human
    content (titles, action-item text, dates, status) lives in Postgres, keyed
    by the id. ``kg_hydrate`` parses each id and attaches a real display name +
    a useful per-type properties payload (so the viewer shows "Q2 Planning",
    and the click panel shows real detail + deep-links). Postgres stays the
    content source of truth, so the graph is fresh even after a rename.

Tenancy / isolation model (mirrors the moat rules):
  * The ``SpeakerProfile`` is resolved ORG-SCOPED *before* any Brigade call —
    a cross-org or missing speaker is a 404 with no existence leak.
  * Hydration is org-scoped: a node whose Postgres row is in another org (or
    deleted) doesn't hydrate, and ``_scope_and_cap`` then drops any non-focus
    node that didn't hydrate — so Brigade is never trusted to scope and a
    cross-org id can't even appear as a bare node.
  * Routing/tenancy props are stripped from every node on the wire.
  * A hard node cap (``KNOWLEDGE_GRAPH_NODE_CAP``, default 250) bounds the
    response + keeps the WebGL viewer comfortable.

Pure render of pre-computed graph data — no LLM — so $0 marginal compute.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user  # noqa: F401
from auth.models import User
from auth.organization import ActiveOrganization
from auth.tier import require_feature
from database.database import get_db
from database.models import SpeakerProfile
from services.brigade_client import BrigadeClient
from services.brigade_writer import _speaker_node_name
from services.kg_hydrate import hydrate_nodes, parse_node_id

# Reuse the per-session shaper: same Brigade payload -> {nodes, links, focus}.
from api.recording import _shape_brigade_graph

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge-graph"])

# Server-side node cap — the real OOM/perf guarantee for the 3D viewer.
_NODE_CAP = int(os.getenv("KNOWLEDGE_GRAPH_NODE_CAP", "250"))

# Person-view ships 1..2 hops (Brigade itself clamps to 1..5).
_MAX_HOPS = 2

# Bound on how many Meeting neighbors we expand for the connected-web pass —
# keeps the parallel fan-out sane for a heavy-meeting person.
_MAX_MEETING_EXPAND = int(os.getenv("KNOWLEDGE_GRAPH_MAX_MEETING_EXPAND", "40"))

# Props never sent to the client (tenancy / graph-routing / Brigade internals).
_STRIPPED_NODE_PROPS = {
    "org_id", "graph", "graph_name", "tenancy_mode", "completion_mode",
    "created_by", "created_at", "distance",
}


async def _fetch_connected_context(
    client: BrigadeClient, focus_node: str, hops: int
) -> Optional[dict[str, Any]]:
    """Focus context at depth 1; for ``hops>=2`` expand each Meeting neighbor in
    parallel and merge its edges so 2-hop nodes connect to their meeting instead
    of floating. Brigade exposes no subgraph query, so this is N+1 parallel.
    """
    raw = await client.fetch_entity_context(
        entity_name=focus_node,
        include_relationships=True,
        include_related=True,
        max_depth=1,
    )
    if raw is None or hops < 2:
        return raw

    meetings: list[str] = []
    for rel in raw.get("relationships") or []:
        for end in (rel.get("from") or {}, rel.get("to") or {}):
            if end.get("type") == "Meeting" and end.get("name") and end["name"] not in meetings:
                meetings.append(end["name"])
    meetings = meetings[:_MAX_MEETING_EXPAND]
    if not meetings:
        return raw

    extra = await asyncio.gather(
        *[
            client.fetch_entity_context(
                entity_name=m, include_relationships=True, include_related=True, max_depth=1
            )
            for m in meetings
        ],
        return_exceptions=True,
    )

    rels = list(raw.get("relationships") or [])
    related = list(raw.get("related_entities") or [])
    for r in extra:
        if isinstance(r, dict):
            if r.get("entity"):
                related.append(r["entity"])
            rels += r.get("relationships") or []
            related += r.get("related_entities") or []

    # Dedup edges by (from, relationship, to) — the focus + per-meeting fetches
    # overlap on the focus->meeting edges.
    seen: set[tuple] = set()
    deduped: list[dict[str, Any]] = []
    for rel in rels:
        key = (
            (rel.get("from") or {}).get("name"),
            rel.get("relationship"),
            (rel.get("to") or {}).get("name"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(rel)

    return {**raw, "relationships": deduped, "related_entities": related}


def _scope_and_cap(shaped: dict[str, Any], *, focus_node: str) -> dict[str, Any]:
    """Keep the focus + the hydrated Meeting-Ops nodes, cap, strip props, drop
    dangling links.

    Hydration is the org gate: a node whose Postgres row is in another org (or
    was deleted) never gets a ``kind`` property, so a non-focus node without one
    is dropped — that both enforces tenant isolation (Brigade isn't trusted to
    scope) and cleans stale/foreign nodes out of the view.
    """
    nodes = shaped.get("nodes") or []
    links = shaped.get("links") or []

    kept: list[dict[str, Any]] = []
    truncated = False
    for node in nodes:
        is_focus = node.get("id") == focus_node or node.get("is_focus")
        props = node.get("properties") or {}
        hydrated = bool(props.get("kind"))
        kind, _, _ = parse_node_id(node.get("id"))
        if not is_focus and not (kind and hydrated):
            continue
        if len(kept) >= _NODE_CAP:
            truncated = True
            continue
        node["properties"] = {k: v for k, v in props.items() if k not in _STRIPPED_NODE_PROPS}
        kept.append(node)

    kept_ids = {n.get("id") for n in kept}
    kept_links = [
        link
        for link in links
        if link.get("source") in kept_ids and link.get("target") in kept_ids
    ]
    return {
        "nodes": kept,
        "links": kept_links,
        "focus": shaped.get("focus"),
        "truncated": truncated,
    }


async def _person_payload(
    db: Session, speaker: SpeakerProfile, hops: int, org_id: int
) -> dict[str, Any]:
    """Build the full {nodes, links, ...} payload for an already-org-resolved
    speaker. Shared by the by-id and /me endpoints."""
    hops = max(1, min(int(hops), _MAX_HOPS))
    speaker_node = _speaker_node_name(speaker.id)

    envelope = {
        "speaker_id": speaker.id,
        "speaker_name": speaker.display_name,
        # Brigade is internal infrastructure, not a customer destination — no
        # "Open in Brigade" deep-link on the in-app graph (the page IS the view).
        "graph_url": None,
        "focus": speaker_node,
        "node_cap": _NODE_CAP,
    }

    client = BrigadeClient()
    client_is_live = client.is_live
    try:
        raw = await _fetch_connected_context(client, speaker_node, hops)
    finally:
        await client.aclose()

    if raw is None:
        # None == "Brigade unreachable" OR "no node for this speaker yet";
        # disambiguate via is_live.
        reason = "live_failed" if client_is_live else "not_synced_yet"
        return {**envelope, "nodes": [], "links": [], "truncated": False, "reason": reason}

    shaped = _shape_brigade_graph(raw, focus_node_id=speaker_node)

    # Avatar for the focus person: Meeting-Ops-local override now; a Contact-Ops
    # federated photo (via speaker.contact_id + CO list_photos) is the next
    # source once the media:read scope is wired — see resolve hook below.
    photo_urls: dict[int, str] = {}
    if speaker.photo_url:
        photo_urls[speaker.id] = speaker.photo_url

    hydrate_nodes(db, shaped["nodes"], organization_id=org_id, photo_urls=photo_urls)
    result = _scope_and_cap(shaped, focus_node=speaker_node)

    # Sparse states read better with explicit copy than an empty 3D canvas.
    meeting_count = sum(
        1 for n in result["nodes"] if (n.get("properties") or {}).get("kind") == "meeting"
    )
    if len(result["nodes"]) <= 1:
        reason: Optional[str] = "not_synced_yet"
    elif meeting_count <= 1:
        reason = "single_meeting"
    else:
        reason = None

    return {**envelope, **result, "reason": reason}


# NOTE: /me MUST be declared before /{speaker_id} — Starlette matches routes in
# order and ``{speaker_id}`` (a path segment) would otherwise capture "me" and
# 422 on the int coercion.
@router.get("/api/knowledge-graph/person/me")
async def get_my_graph(
    hops: int = 2,
    current_user: User = Depends(require_feature("brigade_integration")),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """The logged-in user's OWN person graph, for default-load (no search).

    Resolves the user's speaker by, in order: explicit ``linked_user_id``,
    matching email, then matching display name. Returns ``reason=no_self_speaker``
    (not a 404) when the user has no speaker yet, so the page can fall back to a
    featured person instead of erroring.
    """
    org_id = active_org.organization.id
    base = db.query(SpeakerProfile).filter(SpeakerProfile.organization_id == org_id)

    speaker = base.filter(SpeakerProfile.linked_user_id == current_user.id).first()
    if not speaker and getattr(current_user, "email", None):
        speaker = base.filter(SpeakerProfile.email == current_user.email).first()
    if not speaker and getattr(current_user, "full_name", None):
        speaker = base.filter(SpeakerProfile.display_name == current_user.full_name).first()

    if not speaker:
        return {
            "speaker_id": None,
            "speaker_name": getattr(current_user, "full_name", None)
            or getattr(current_user, "username", None),
            "nodes": [],
            "links": [],
            "focus": None,
            "node_cap": _NODE_CAP,
            "reason": "no_self_speaker",
        }

    return await _person_payload(db, speaker, hops, org_id)


@router.get("/api/knowledge-graph/person/{speaker_id}")
async def get_person_graph(
    speaker_id: int,
    hops: int = 2,
    current_user: User = Depends(require_feature("brigade_integration")),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A speaker's cross-meeting subgraph from Brigade, org-scoped + hydrated.

    Response: ``{speaker_id, speaker_name, nodes, links, focus, graph_url,
    node_cap, truncated, reason}`` where ``reason`` is one of
    ``not_synced_yet`` / ``single_meeting`` / ``no_self_speaker`` / ``live_failed`` / ``None``.
    """
    org_id = active_org.organization.id

    # Tenancy boundary: resolve the speaker ORG-SCOPED first. Cross-org or
    # missing -> 404, no existence leak (mirrors the per-session endpoint).
    speaker = (
        db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.id == speaker_id,
            SpeakerProfile.organization_id == org_id,
        )
        .first()
    )
    if not speaker:
        raise HTTPException(status_code=404, detail="Speaker not found")

    return await _person_payload(db, speaker, hops, org_id)
