"""Hydrate bare Brigade graph nodes with real content from Postgres.

Brigade stores the graph *structure* (who connects to what) but the nodes it
returns from ``fetch_entity_context`` carry only ``name`` (the canonical id,
e.g. ``meeting_ops_meeting_102``) and ``type`` — no titles, text, dates, or
status. The human-readable *content* lives in Postgres, keyed by the id.

This module parses each node id back to its Postgres row and attaches a real
display ``name`` + a useful, per-type ``properties`` payload (so the 3D viewer
shows "Q2 Planning" instead of "meeting_ops_meeting_102", and the click panel
shows action-item text / status / dates / deep-links). Postgres is the content
source of truth, so the graph stays fresh even if a meeting is renamed after
the Brigade node was written.

Everything is ORG-SCOPED: a node whose Postgres row belongs to a different org
(or doesn't exist) is left bare rather than hydrated — defense in depth on top
of the endpoint's org filter.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from database.models import ActionItem, RecordingSession, SpeakerProfile

logger = logging.getLogger(__name__)

# meeting_ops_<kind>_<pk>  OR  meeting_ops_<kind>_<session_pk>_<idx>
_ID_RE = re.compile(r"^meeting_ops_(speaker|meeting|action|topic|decision)_(\d+)(?:_(\d+))?$")


def parse_node_id(node_id: Optional[str]) -> tuple[Optional[str], Optional[int], Optional[int]]:
    """('speaker'|'meeting'|'action'|'topic'|'decision', pk, idx) or (None, None, None)."""
    m = _ID_RE.match(node_id or "")
    if not m:
        return (None, None, None)
    return (m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) is not None else None)


def _truncate(text: Optional[str], n: int = 80) -> Optional[str]:
    if not text:
        return None
    text = " ".join(text.split())
    return (text[: n - 1] + "…") if len(text) > n else text


def hydrate_nodes(
    db: Session,
    nodes: list[dict[str, Any]],
    *,
    organization_id: int,
    photo_urls: Optional[dict[int, str]] = None,
) -> list[dict[str, Any]]:
    """Mutate ``nodes`` in place: set a human ``name`` + useful ``properties``
    per node from Postgres. Unparseable / cross-org / missing rows keep their
    existing (bare) name. Returns the same list for chaining.

    ``photo_urls`` optionally overrides a speaker's avatar (e.g. a Contact-Ops
    federated photo the endpoint resolved for the focus person); a speaker's
    own ``photo_url`` column (the MO-local override) is used otherwise.
    """
    photo_urls = photo_urls or {}

    # --- collect the pks we need, by kind ---
    speaker_ids: set[int] = set()
    action_ids: set[int] = set()
    session_ids: set[int] = set()  # meetings + the parent sessions of topics/decisions
    parsed: dict[str, tuple[Optional[str], Optional[int], Optional[int]]] = {}
    for node in nodes:
        kind, pk, idx = parse_node_id(node.get("id"))
        parsed[node["id"]] = (kind, pk, idx)
        if pk is None:
            continue
        if kind == "speaker":
            speaker_ids.add(pk)
        elif kind == "meeting":
            session_ids.add(pk)
        elif kind == "action":
            action_ids.add(pk)
        elif kind in ("topic", "decision"):
            session_ids.add(pk)  # pk is the parent session for these

    # --- batch load, all org-scoped ---
    speakers: dict[int, SpeakerProfile] = (
        {s.id: s for s in db.query(SpeakerProfile).filter(
            SpeakerProfile.id.in_(speaker_ids),
            SpeakerProfile.organization_id == organization_id,
        ).all()} if speaker_ids else {}
    )
    sessions: dict[int, RecordingSession] = (
        {m.id: m for m in db.query(RecordingSession).filter(
            RecordingSession.id.in_(session_ids),
            RecordingSession.organization_id == organization_id,
        ).all()} if session_ids else {}
    )
    actions: dict[int, ActionItem] = (
        {a.id: a for a in db.query(ActionItem).filter(
            ActionItem.id.in_(action_ids),
            ActionItem.organization_id == organization_id,
        ).all()} if action_ids else {}
    )

    # topic/decision text is JSON inside the parent session's final_summary;
    # reuse the writer's extractors so naming matches what got written.
    try:
        from services.brigade_writer import _extract_decisions, _extract_topics
    except Exception:  # pragma: no cover - defensive
        _extract_topics = _extract_decisions = lambda _s: []  # type: ignore

    for node in nodes:
        kind, pk, idx = parsed.get(node["id"], (None, None, None))
        if pk is None:
            continue
        props: dict[str, Any] = {}

        if kind == "speaker":
            s = speakers.get(pk)
            if not s:
                continue
            node["name"] = s.display_name or node.get("name")
            props = {
                "kind": "speaker",
                "speaker_id": s.id,
                "email": s.email,
                "company": s.company,
                "role": s.title,
            }
            avatar = photo_urls.get(s.id) or s.photo_url
            if avatar:
                props["avatar_url"] = avatar
            if s.linked_user_id:
                props["linked_user_id"] = s.linked_user_id

        elif kind == "meeting":
            m = sessions.get(pk)
            if not m:
                continue
            date_iso = m.started_at.isoformat() if m.started_at else None
            label = m.title or m.name
            if not label:
                label = f"Meeting · {m.started_at:%b %d, %Y}" if m.started_at else f"Meeting {pk}"
            node["name"] = label
            props = {
                "kind": "meeting",
                "session_pk": m.id,
                "session_id": m.session_id,  # uuid for the /sessions/<id> deep-link
                "date": date_iso,
                "status": m.status,
                "duration_min": round((m.duration or 0) / 60, 1) if m.duration else None,
            }

        elif kind == "action":
            a = actions.get(pk)
            if not a:
                continue
            node["name"] = _truncate(a.text, 80) or f"Action {pk}"
            props = {
                "kind": "action_item",
                "text": a.text,
                "owner": a.owner,
                "status": a.status,
                "due_date": a.due_date.isoformat() if a.due_date else None,
                "source_session_pk": a.session_id,
            }

        elif kind in ("topic", "decision"):
            m = sessions.get(pk)
            text = None
            decided_by = None
            if m and idx is not None:
                summary = m.final_summary or {}
                items = _extract_topics(summary) if kind == "topic" else _extract_decisions(summary)
                if 0 <= idx < len(items):
                    text = items[idx].get("text")
                    decided_by = items[idx].get("decided_by")
            node["name"] = _truncate(text, 70) or (
                f"{kind.capitalize()} · {m.title}" if m and m.title else kind.capitalize()
            )
            props = {
                "kind": kind,
                "text": text,
                "parent_session_pk": pk,
            }
            if m:
                props["parent_meeting"] = m.title or m.name
            if decided_by:
                props["decided_by"] = decided_by

        # drop None values for a clean detail panel; keep falsey-but-meaningful
        node["properties"] = {k: v for k, v in props.items() if v is not None}

    return nodes
