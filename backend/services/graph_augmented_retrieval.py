"""Read-only graph augmentation for meeting RAG.

This module keeps the existing Qdrant-first retriever as the seed layer and
adds a read-only Brigade neighborhood expansion on top. It does not mutate any
graph state.

Flow:
  1. Seed candidate meetings from ``SemanticSearchService.search``.
  2. Pull the Brigade neighborhood for those meetings.
  3. Extract salient entities from the query and top chunks.
  4. Expand via matching speaker nodes, then pull their meeting neighborhoods.
  5. Re-rank meetings with a modest graph bonus and return an evidence bundle
     the meeting-rag prompt can consume.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from database.models import RecordingSession
from services.brigade_client import BrigadeClient
from services.semantic_search_service import SemanticSearchService

logger = logging.getLogger(__name__)

_DEFAULT_SEED_LIMIT = 5
_MAX_EXPANSION_SEEDS = 8
_MAX_GRAPH_BONUS = 1.5
_GRAPH_ENTITY_RE = re.compile(
    r"\b("
    r"[A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+)*"
    r"|[A-Z][A-Z0-9]{2,}"
    r"|[A-Z][a-zA-Z0-9]*\d+[a-zA-Z0-9]*"
    r")\b"
)
_STOPWORDS = {
    "the",
    "and",
    "or",
    "for",
    "with",
    "about",
    "what",
    "did",
    "across",
    "meetings",
    "meeting",
    "session",
    "this",
    "that",
    "from",
    "into",
    "your",
    "you",
    "i",
    "we",
    "my",
    "our",
}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on", "enabled"}


def resolve_graph_augmentation_enabled(
    scope: Optional[dict[str, Any]] = None,
) -> bool:
    """Resolve the feature flag for graph augmentation.

    Per-request scope wins over the env default so callers can A/B the
    behavior without changing deployment config.
    """
    if isinstance(scope, dict):
        for key in (
            "graph_augmented_retrieval",
            "graph_augment",
            "rag_graph_augmentation",
        ):
            if key in scope:
                return _truthy(scope.get(key))

    return _truthy(os.getenv("MEETING_RAG_GRAPH_AUGMENTATION", "0"))


def _normalize_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", (text or "").lower())
    return " ".join(cleaned.split())


def _candidate_phrases(text: str) -> list[str]:
    phrases: list[str] = []
    if not text:
        return phrases
    for match in _GRAPH_ENTITY_RE.finditer(text):
        phrase = match.group(1).strip()
        if phrase and phrase.lower() not in _STOPWORDS:
            phrases.append(phrase)
    return phrases


def _extract_salient_entities(
    query: str,
    base_results: list[dict[str, Any]],
    chunk_hits: list[dict[str, Any]],
) -> list[str]:
    """Best-effort query/entity extraction.

    This is intentionally lightweight: the graph layer should bias the
    existing retriever, not replace it with a new NER stack.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def add(value: Optional[str]) -> None:
        if not value:
            return
        norm = _normalize_text(value)
        if not norm or norm in seen:
            return
        if norm in _STOPWORDS:
            return
        seen.add(norm)
        ordered.append(value.strip())

    for phrase in _candidate_phrases(query):
        add(phrase)

    for hit in base_results[:5]:
        add(hit.get("title"))
        snippet = hit.get("snippet") or ""
        for phrase in _candidate_phrases(str(snippet)[:400]):
            add(phrase)

    for chunk in chunk_hits[:8]:
        for speaker in chunk.get("speakers") or []:
            add(str(speaker))
        for phrase in _candidate_phrases(str(chunk.get("text") or "")[:400]):
            add(phrase)

    return ordered[:_MAX_EXPANSION_SEEDS]


def _meeting_node_name(session_pk: int) -> str:
    return f"meeting_ops_meeting_{session_pk}"


def _speaker_node_name(speaker_id: Any) -> Optional[str]:
    try:
        return f"meeting_ops_speaker_{int(speaker_id)}"
    except (TypeError, ValueError):
        return None


def _parse_session_pk(node_name: Optional[str]) -> Optional[int]:
    if not node_name:
        return None
    match = re.match(r"^meeting_ops_meeting_(\d+)$", str(node_name))
    if not match:
        return None
    return int(match.group(1))


def _node_properties(node: dict[str, Any]) -> dict[str, Any]:
    props = node.get("properties")
    if isinstance(props, dict):
        return props
    return {}


def _node_org_id(node: dict[str, Any]) -> Optional[int]:
    props = _node_properties(node)
    org_id = props.get("org_id")
    try:
        return int(org_id) if org_id is not None else None
    except (TypeError, ValueError):
        return None


def _meeting_identity_from_context(context: dict[str, Any]) -> tuple[Optional[int], Optional[str], Optional[str]]:
    entity = context.get("entity") or {}
    if not isinstance(entity, dict):
        entity = {}
    props = _node_properties(entity)
    node_name = entity.get("name") or props.get("name")
    session_pk = props.get("id")
    if session_pk is None:
        session_pk = _parse_session_pk(node_name)
    try:
        session_pk = int(session_pk) if session_pk is not None else None
    except (TypeError, ValueError):
        session_pk = None
    title = props.get("title") or entity.get("title") or entity.get("label")
    return session_pk, node_name, title


def _resolve_session_row(
    db: Session,
    org_id: int,
    session_id: str | int,
) -> Optional[RecordingSession]:
    q = db.query(RecordingSession).filter(RecordingSession.organization_id == org_id)
    row = q.filter(RecordingSession.session_id == str(session_id)).first()
    if row is not None:
        return row
    try:
        pk = int(session_id)
    except (TypeError, ValueError):
        return None
    return q.filter(RecordingSession.id == pk).first()


def _collect_related_nodes(context: dict[str, Any], *, node_type: Optional[str] = None) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}

    entity = context.get("entity")
    if isinstance(entity, dict):
        name = entity.get("name")
        if name:
            nodes[str(name)] = entity

    for rel in context.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        for endpoint_key in ("from", "to"):
            endpoint = rel.get(endpoint_key)
            if not isinstance(endpoint, dict):
                continue
            name = endpoint.get("name")
            if not name:
                continue
            endpoint_type = endpoint.get("type")
            if node_type and endpoint_type != node_type:
                continue
            nodes.setdefault(str(name), endpoint)

    for related in context.get("related_entities") or []:
        if not isinstance(related, dict):
            continue
        name = related.get("name")
        if not name:
            continue
        if node_type and related.get("type") != node_type:
            continue
        nodes.setdefault(str(name), related)

    return list(nodes.values())


def _relationship_names(context: dict[str, Any], node_type: Optional[str] = None) -> list[str]:
    names: list[str] = []
    for rel in context.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        for endpoint_key in ("from", "to"):
            endpoint = rel.get(endpoint_key)
            if not isinstance(endpoint, dict):
                continue
            if node_type and endpoint.get("type") != node_type:
                continue
            name = endpoint.get("name")
            if name:
                names.append(str(name))
    return names


def _speaker_matches_phrase(phrase: str, speaker_name: Optional[str], speaker_label: Optional[str]) -> bool:
    norm_phrase = _normalize_text(phrase)
    if not norm_phrase:
        return False
    candidates = [_normalize_text(speaker_name or ""), _normalize_text(speaker_label or "")]
    for candidate in candidates:
        if not candidate:
            continue
        if norm_phrase == candidate:
            return True
        if norm_phrase in candidate or candidate in norm_phrase:
            return True
        phrase_terms = set(norm_phrase.split())
        candidate_terms = set(candidate.split())
        if phrase_terms and candidate_terms and len(phrase_terms & candidate_terms) / len(phrase_terms) >= 0.5:
            return True
    return False


def _title_boost(query: str, title: str) -> float:
    """Score how directly a meeting's own title matches the query.

    Mirrors ``SemanticSearchService._title_boost`` (v3.10.1): +2.5 exact,
    +1.5 substring, +1.0 at >=75% term overlap, +0.5 at >=50%. Kept local so
    the graph layer can reason about direct-text strength without importing
    the Qdrant service (which tests routinely monkeypatch). Keep the
    magnitudes in sync with that method.
    """
    nq = _normalize_text(query)
    nt = _normalize_text(title)
    if not nq or not nt:
        return 0.0
    if nq == nt:
        return 2.5
    if nq in nt or nt in nq:
        return 1.5
    query_terms = set(nq.split())
    title_terms = set(nt.split())
    if not query_terms or not title_terms:
        return 0.0
    overlap = len(query_terms & title_terms) / len(query_terms)
    if overlap >= 0.75:
        return 1.0
    if overlap >= 0.5:
        return 0.5
    return 0.0


def _final_score(base_score: float, title_boost: float, graph_bonus: float) -> float:
    """Compose base + title + graph with a direct-text dominance rule.

    A meeting whose own title clearly matches the query already carries a
    strong, deterministic signal, so the graph bonus must not re-rank it
    beneath a merely graph-adjacent sibling. When the title boost is high the
    graph bonus collapses to a tiebreaker. When there is no direct-text signal
    (``title_boost == 0``) the full graph bonus (up to ``_MAX_GRAPH_BONUS``)
    applies, preserving the original intent of surfacing meetings linked via
    speakers / topics / decisions.
    """
    if title_boost >= 1.0:
        # Exact / substring / >=75%-overlap title: graph is a tiebreaker only.
        graph_bonus = min(graph_bonus, 0.25)
    elif title_boost >= 0.5:
        # Partial (>=50%) title overlap: allow a small graph nudge.
        graph_bonus = min(graph_bonus, 0.5)
    # else: no direct-text signal -- full graph bonus up to _MAX_GRAPH_BONUS.
    return base_score + title_boost + graph_bonus


def _meeting_bonus(
    query: str,
    title: str,
    *,
    seed_score: float,
    graph_hits: list[str],
    related_text: list[str],
) -> float:
    bonus = 0.0
    nq = _normalize_text(query)
    nt = _normalize_text(title)
    if nq and nt:
        if nq == nt:
            bonus += 1.0
        elif nq in nt or nt in nq:
            bonus += 0.6
        else:
            query_terms = set(nq.split())
            title_terms = set(nt.split())
            if query_terms and title_terms:
                overlap = len(query_terms & title_terms) / len(query_terms)
                if overlap >= 0.75:
                    bonus += 0.5
                elif overlap >= 0.5:
                    bonus += 0.25

    if graph_hits:
        bonus += min(0.75, 0.175 * len(graph_hits))

    if related_text:
        query_terms = {t for t in nq.split() if len(t) > 3 and t not in _STOPWORDS}
        if query_terms:
            related_blob = _normalize_text(" ".join(related_text))
            overlap = len(query_terms & set(related_blob.split()))
            if overlap:
                bonus += min(0.5, overlap * 0.1)

    # Never let the graph swamp the base retriever. Magnitudes are rebased
    # below the title-boost ceiling (2.5) so a direct-text match always
    # out-ranks a purely graph-adjacent sibling; the per-meeting composition
    # in augment_meeting_search applies _final_score's dominance cap on top.
    return min(_MAX_GRAPH_BONUS, max(0.0, bonus))


def _format_graph_block(
    *,
    meeting_title: str,
    seed: bool,
    query_hits: list[str],
    linked_meetings: list[str],
    related_speakers: list[str],
    related_text: list[str],
) -> str:
    lines = [f"Graph context for {meeting_title}:"]
    if seed:
        lines.append("Seed meeting from Qdrant.")
    if query_hits:
        lines.append("Query/entity matches: " + "; ".join(query_hits[:6]))
    if related_speakers:
        lines.append("Linked speakers: " + "; ".join(related_speakers[:6]))
    if linked_meetings:
        lines.append("Linked meetings: " + "; ".join(linked_meetings[:6]))
    if related_text:
        lines.append("Graph notes: " + "; ".join(related_text[:6]))
    return "\n".join(lines)


async def augment_meeting_search(
    db: Session,
    org_id: int,
    *,
    query: str,
    base_results: list[dict[str, Any]],
    limit: int = _DEFAULT_SEED_LIMIT,
    chunk_limit: int = 8,
    enabled: bool = True,
) -> dict[str, Any]:
    """Augment base meeting search results with Brigade graph evidence."""
    normalized_results = list(base_results or [])
    if not enabled:
        return {
            "enabled": False,
            "query": query,
            "results": normalized_results[:limit],
            "graph": {
                "seed_sessions": [],
                "seed_entities": [],
                "expanded_entities": [],
                "linked_meetings": [],
                "evidence_by_session": {},
            },
        }

    seed_session_ids: list[str] = []
    session_rows: list[RecordingSession] = []
    for hit in normalized_results[: max(limit, _DEFAULT_SEED_LIMIT)]:
        sid = hit.get("session_id")
        if sid is None:
            continue
        row = _resolve_session_row(db, org_id, sid)
        if row is None:
            continue
        seed_session_ids.append(str(row.session_id or row.id))
        session_rows.append(row)

    if not session_rows:
        return {
            "enabled": True,
            "query": query,
            "results": normalized_results[:limit],
            "graph": {
                "seed_sessions": [],
                "seed_entities": [],
                "expanded_entities": [],
                "linked_meetings": [],
                "evidence_by_session": {},
            },
        }

    semantic = SemanticSearchService()
    try:
        chunk_hits = semantic.search_chunks(
            query,
            limit=chunk_limit,
            organization_id=org_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("graph augmentation chunk search failed: %s", exc)
        chunk_hits = []

    salient_entities = _extract_salient_entities(query, normalized_results, chunk_hits)

    entity_names = [_meeting_node_name(row.id) for row in session_rows]
    seed_contexts: dict[str, dict[str, Any]] = {}
    evidence_by_session: dict[str, dict[str, Any]] = {}
    matched_speaker_nodes: dict[str, dict[str, Any]] = {}
    linked_meeting_nodes: dict[str, dict[str, Any]] = {}

    async with BrigadeClient() as brigade:
        seed_contexts_raw = await asyncio.gather(
            *[
                brigade.fetch_entity_context(
                    entity_name=name,
                    include_relationships=True,
                    include_related=True,
                    max_depth=1,
                )
                for name in entity_names
            ]
        )
        for row, node_name, context in zip(session_rows, entity_names, seed_contexts_raw):
            if not context:
                continue
            seed_contexts[node_name] = context
            sid = str(row.session_id or row.id)
            evidence_by_session.setdefault(sid, {
                "session_id": sid,
                "title": row.title or row.name or f"Session {row.id}",
                "seed": True,
                "query_hits": [],
                "related_speakers": [],
                "linked_meetings": [],
                "related_text": [],
                "graph_bonus": 0.0,
            })

            speaker_nodes = _collect_related_nodes(context, node_type="Speaker")
            for speaker in speaker_nodes:
                speaker_id = speaker.get("name")
                speaker_display = (
                    _node_properties(speaker).get("display_name")
                    or speaker.get("display_name")
                    or speaker.get("label")
                    or speaker_id
                )
                if not speaker_id:
                    continue
                speaker_node_name = str(speaker_id)
                for phrase in salient_entities or [query]:
                    if _speaker_matches_phrase(
                        phrase,
                        speaker_node_name,
                        str(speaker_display or ""),
                    ):
                        matched_speaker_nodes[speaker_node_name] = speaker
                        evidence_by_session[sid]["query_hits"].append(phrase)
                        break
                # Capture speaker names for the prompt regardless of
                # whether they match, as they help disambiguate follow-up.
                speaker_label = str(speaker_display)
                if speaker_label not in evidence_by_session[sid]["related_speakers"]:
                    evidence_by_session[sid]["related_speakers"].append(speaker_label)

            # Topics / decisions / direct meeting-related text are useful
            # prompt hints even when they do not seed further expansion.
            for related in _collect_related_nodes(context):
                related_type = related.get("type")
                if related_type in {"Topic", "Decision"}:
                    text = _node_properties(related).get("text") or related.get("text")
                    if text:
                        text_value = str(text).strip()
                        if text_value and text_value not in evidence_by_session[sid]["related_text"]:
                            evidence_by_session[sid]["related_text"].append(text_value)

    # Expand from the matched speaker nodes to linked meetings.
    speaker_contexts: dict[str, dict[str, Any]] = {}
    if matched_speaker_nodes:
        async with BrigadeClient() as brigade:
            speaker_contexts_raw = await asyncio.gather(
                *[
                    brigade.fetch_entity_context(
                        entity_name=name,
                        include_relationships=True,
                        include_related=True,
                        max_depth=1,
                    )
                    for name in matched_speaker_nodes
                ]
            )
        for speaker_name, context in zip(matched_speaker_nodes, speaker_contexts_raw):
            if not context:
                continue
            speaker_contexts[speaker_name] = context
            for related in _collect_related_nodes(context, node_type="Meeting"):
                props = _node_properties(related)
                session_pk = props.get("id")
                if session_pk is None:
                    session_pk = _parse_session_pk(related.get("name"))
                try:
                    session_pk = int(session_pk) if session_pk is not None else None
                except (TypeError, ValueError):
                    session_pk = None
                if session_pk is None:
                    continue
                row = _resolve_session_row(db, org_id, session_pk)
                if row is None:
                    continue
                sid = str(row.session_id or row.id)
                linked_meeting_nodes[str(related.get("name") or _meeting_node_name(session_pk))] = related
                evidence = evidence_by_session.setdefault(sid, {
                    "session_id": sid,
                    "title": row.title or row.name or f"Session {row.id}",
                    "seed": False,
                    "query_hits": [],
                    "related_speakers": [],
                    "linked_meetings": [],
                    "related_text": [],
                    "graph_bonus": 0.0,
                })
                evidence["linked_meetings"].append({
                    "session_id": sid,
                    "title": row.title or row.name or f"Session {row.id}",
                    "via": speaker_name,
                })

    # Score and assemble final result list.
    scored: dict[str, dict[str, Any]] = {}
    for hit in normalized_results:
        sid = str(hit.get("session_id"))
        title = hit.get("title") or ""
        evidence = evidence_by_session.get(sid)
        graph_hits: list[str] = []
        related_text: list[str] = []
        if evidence:
            graph_hits.extend(e["title"] for e in evidence.get("linked_meetings", []))
            graph_hits.extend(evidence.get("related_speakers", []))
            related_text.extend(evidence.get("related_text", []))
        base_score = float(hit.get("score") or 0.0)
        title_boost = _title_boost(query, title)
        graph_bonus = _meeting_bonus(
            query,
            title,
            seed_score=base_score,
            graph_hits=graph_hits,
            related_text=related_text,
        )
        # The semantic base score already folds in the title boost
        # (SemanticSearchService.search), so peel it back out and let
        # _final_score re-add it exactly once alongside the (capped) graph
        # bonus. Net effect for a seed: score = base_score + capped_graph.
        final = _final_score(base_score - title_boost, title_boost, graph_bonus)
        effective_bonus = max(0.0, round(final - base_score, 4))
        augmented = dict(hit)
        augmented["graph_bonus"] = effective_bonus
        augmented["score"] = round(final, 4)
        augmented["match_type"] = "graph_augmented" if effective_bonus else hit.get("match_type", "semantic")
        if evidence:
            augmented["graph_evidence"] = evidence
        current = scored.get(sid)
        if current is None or augmented["score"] > current["score"]:
            scored[sid] = augmented

    # Add graph-discovered meetings that were not in the seed hit list.
    for sid, evidence in evidence_by_session.items():
        if sid in scored:
            continue
        row = _resolve_session_row(db, org_id, sid)
        if row is None:
            continue
        title = row.title or row.name or f"Session {row.id}"
        graph_hits = [e["title"] for e in evidence.get("linked_meetings", [])] + evidence.get("related_speakers", [])
        title_boost = _title_boost(query, title)
        graph_bonus = _meeting_bonus(
            query,
            title,
            seed_score=0.0,
            graph_hits=graph_hits,
            related_text=evidence.get("related_text", []),
        )
        # Graph-discovered meetings have no Qdrant base score, so give them
        # their own direct-text (title) credit. This is what lets an exact-
        # title meeting reached only through the graph still out-rank a
        # graph-adjacent sibling (the session-122 regression).
        final = _final_score(0.0, title_boost, graph_bonus)
        effective_bonus = max(0.0, round(final - title_boost, 4))
        scored[sid] = {
            "session_id": sid,
            "title": title,
            "score": round(final, 4),
            "match_type": "graph_discovered" if (effective_bonus or title_boost) else "graph_augmented",
            "snippet": row.summary or row.transcript_simple or "",
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "graph_bonus": effective_bonus,
            "graph_evidence": evidence,
        }

    results = sorted(
        scored.values(),
        key=lambda item: float(item.get("score") or 0.0),
        reverse=True,
    )[:limit]

    # Enrich evidence with natural-language graph blocks for prompts/debugging.
    for result in results:
        sid = str(result.get("session_id"))
        evidence = evidence_by_session.get(sid)
        if not evidence:
            continue
        title = result.get("title") or evidence.get("title") or sid
        query_hits = _extract_salient_entities(query, [result], chunk_hits)
        evidence["graph_block"] = _format_graph_block(
            meeting_title=str(title),
            seed=bool(evidence.get("seed")),
            query_hits=query_hits,
            linked_meetings=[m["title"] for m in evidence.get("linked_meetings", [])],
            related_speakers=evidence.get("related_speakers", []),
            related_text=evidence.get("related_text", []),
        )
        result["graph_evidence"] = evidence

    return {
        "enabled": True,
        "query": query,
        "results": results,
        "graph": {
            "seed_sessions": seed_session_ids,
            "seed_entities": salient_entities,
            "expanded_entities": list(matched_speaker_nodes.keys()),
            "linked_meetings": list(linked_meeting_nodes.keys()),
            "evidence_by_session": evidence_by_session,
        },
    }


__all__ = [
    "augment_meeting_search",
    "resolve_graph_augmentation_enabled",
]
