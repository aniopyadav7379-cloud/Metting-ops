"""Best-effort graph writer: Meeting-Ops -> Unicorn Brigade.

Public surface:

  await write_meeting_to_brigade(session_id, db, client=None, completion_mode="reprocess")

Called from two paths:

  * ``api.recording._run_session_reprocess`` (completion_mode="reprocess")
    after the existing Parakeet 1.1B + pyannote + Qwen 3.6 pipeline.
  * ``api.recording.finalize_always_on_session`` (completion_mode="live")
    when a browser/live session is finalized without the reprocess pass.

The writer:

  1. Resolves the active tenancy mode + graph name from env.
  2. Pulls the session + its action items + speaker links + summary
     from Postgres (single read, no joins across orgs).
  3. Writes one :Meeting node, N :Speaker / :ActionItem / :Topic /
     :Decision nodes, and the corresponding edges, in dependency order
     (entities before relationships).
  4. Stamps ``recording_sessions.brigade_synced_at`` + ``brigade_graph_node_id``
     on success so the frontend can render the "View in Brigade graph"
     link and the reconciliation job knows which sessions to skip.

All Brigade calls are idempotent (MERGE-shaped on the Brigade side via
entity ``name`` keys). Re-running the writer on an already-synced
session is safe — properties update, no duplicates.

Failure mode contract: the writer **never** raises into the calling
pipeline. Every exception is logged + swallowed. If Brigade is down
the writer logs once at WARNING per call and returns; the
reconciliation job (``scripts/reconcile_brigade_graph.py`` — Phase 1
ticket P1-RECONCILE-JOB) walks unsynced sessions later. This protects
the recording UX, which must complete even when downstream services
are degraded.

Tenancy modes (per docs/brigade-integration-design.md section 8):

  shared            Single graph for all orgs. agent_id passed to
                    Brigade is ``meeting_ops_canonical``. Brigade
                    derives graph name ``agent_meeting_ops_canonical``.
                    Org isolation is by ``org_id`` property on every
                    node + WHERE clause on reads (Phase 2 surface).

  per_org_graph     One Brigade graph per org. agent_id is
                    ``meeting_ops_org_{org_id}`` -> graph
                    ``agent_meeting_ops_org_{org_id}``. Org isolation
                    is structural (different graphs can't see each
                    other's nodes from Cypher MATCH).

  per_org_instance  Different Brigade *instance* per org. Phase 1
                    leaves this as the same code path with a different
                    ``BRIGADE_API_BASE_URL`` resolved per-deployment;
                    the actual per-org URL resolver is infra-level
                    config and lands in Phase 5.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from services.brigade_client import BrigadeClient, BrigadeWriteResult


logger = logging.getLogger(__name__)


# Tenancy mode literal strings (per docs/brigade-integration-design.md).
# Kept here as module-level constants so the call sites can import them
# in tests without colliding with random string literals.
TENANCY_SHARED = "shared"
TENANCY_PER_ORG_GRAPH = "per_org_graph"
TENANCY_PER_ORG_INSTANCE = "per_org_instance"

VALID_TENANCY_MODES = {
    TENANCY_SHARED,
    TENANCY_PER_ORG_GRAPH,
    TENANCY_PER_ORG_INSTANCE,
}

DEFAULT_TENANCY_MODE = TENANCY_PER_ORG_GRAPH
DEFAULT_SHARED_GRAPH_BASENAME = "meeting_ops_canonical"
PER_ORG_GRAPH_PREFIX = "meeting_ops_org"

# Completion mode values for the ``completion_mode`` Meeting node property.
# ``reprocess`` = full Parakeet 1.1B + pyannote + Qwen 3.6 pipeline.
# ``live`` = browser/live finalize without diarization or topic extraction.
COMPLETION_MODE_REPROCESS = "reprocess"
COMPLETION_MODE_LIVE = "live"

# Conceptual entity_type labels we write to Brigade. These show up as
# ``properties.entity_type`` on the FalkorDB :Entity node; Brigade's
# label color map already knows the common ones (Person, Concept, etc).
# Meeting / ActionItem / Topic / Decision are first-class for us.
ENTITY_TYPE_MEETING = "Meeting"
ENTITY_TYPE_SPEAKER = "Speaker"
ENTITY_TYPE_ACTION_ITEM = "ActionItem"
ENTITY_TYPE_TOPIC = "Topic"
ENTITY_TYPE_DECISION = "Decision"

# Edge labels — uppercase per Cypher convention.
REL_HAS_SPEAKER = "HAS_SPEAKER"
REL_HAS_ACTION_ITEM = "HAS_ACTION_ITEM"
REL_HAS_TOPIC = "HAS_TOPIC"
REL_HAS_DECISION = "HAS_DECISION"
REL_ASSIGNED_TO = "ASSIGNED_TO"
REL_DECIDED_BY = "DECIDED_BY"


@dataclass(frozen=True)
class _GraphTarget:
    """Resolved tenancy target for a single write batch."""

    mode: str
    # The agent_id we pass to Brigade's store endpoints. This drives
    # graph routing on Brigade's side via _get_agent_graph_name.
    agent_id: str
    # Human-friendly graph name string (the one Brigade will derive
    # from agent_id). Surfaced to the frontend "View in Brigade graph"
    # link via the URL builder.
    graph_name: str


def _resolve_tenancy(
    org_id: int,
    *,
    tenancy_mode_override: Optional[str] = None,
    shared_agent_id_override: Optional[str] = None,
    strict: bool = False,
) -> _GraphTarget:
    """Compute the active tenancy target from per-org config + org_id.

    Per-org overrides (passed in from the brigade integration config on
    Organization.integrations.brigade) win over the env-var defaults.

    On an UNKNOWN mode the behaviour depends on ``strict``:

    - ``strict=True`` (the WRITE path) → **raise**. An invalid mode is
      almost always an operator typo (e.g. ``per_org_graphs``); silently
      falling back to ``shared`` would write *every* org's meetings into
      one graph — the exact cross-org commingling the operator was trying
      to avoid. We fail closed: the writer's top-level ``except`` turns
      this into a logged, skipped write (no data, no commingling) until
      the env is fixed.
    - ``strict=False`` (READ / deep-link paths) → warn + fall back to
      ``shared``. These paths are downstream-org-scoped anyway (KG node
      hydration filters by ``organization_id``), so a wrong graph name
      degrades to "your own data / empty", never a cross-org read — and a
      hard 500 on a read would be a worse outcome than the safe fallback.

    Note an *unset* env is not "unknown": ``os.getenv`` returns
    ``DEFAULT_TENANCY_MODE`` (a valid mode), so the default posture is
    unaffected — only an explicit, invalid value trips the strict path.
    """
    raw_mode = (
        (tenancy_mode_override or os.getenv("BRIGADE_TENANCY_MODE", DEFAULT_TENANCY_MODE))
        .strip()
        .lower()
    )
    if raw_mode not in VALID_TENANCY_MODES:
        if strict:
            raise ValueError(
                f"invalid BRIGADE_TENANCY_MODE={raw_mode!r}; expected one of "
                f"{sorted(VALID_TENANCY_MODES)}. Refusing the Brigade write "
                f"(fail-closed) to avoid cross-org commingling — fix the env."
            )
        logger.warning(
            "brigade_writer unknown BRIGADE_TENANCY_MODE=%r; falling back to %s "
            "(read path — downstream org-scoping keeps this safe)",
            raw_mode,
            DEFAULT_TENANCY_MODE,
        )
        raw_mode = DEFAULT_TENANCY_MODE

    if raw_mode == TENANCY_SHARED:
        agent_id = (
            shared_agent_id_override
            or os.getenv(
                "BRIGADE_SHARED_AGENT_ID", DEFAULT_SHARED_GRAPH_BASENAME
            )
        )
    elif raw_mode == TENANCY_PER_ORG_GRAPH:
        agent_id = f"{PER_ORG_GRAPH_PREFIX}_{org_id}"
    else:  # per_org_instance — same agent_id shape, different base URL
        agent_id = f"{PER_ORG_GRAPH_PREFIX}_{org_id}"

    # Brigade's graph_manager._get_agent_graph_name replaces '-' with '_'
    # and prefixes 'agent_'. Mirror that derivation locally for the
    # frontend deep-link.
    derived_graph = f"agent_{agent_id.replace('-', '_')}"
    return _GraphTarget(
        mode=raw_mode, agent_id=agent_id, graph_name=derived_graph
    )


def _meeting_node_name(session_pk: int) -> str:
    """Canonical entity name (= Brigade MERGE key) for a Meeting node.

    Org_id is intentionally NOT part of the name in shared mode because
    sessions migrate between orgs (session_move_org); the writer
    updates ``properties.org_id`` on re-write rather than orphaning the
    old key. In per_org_graph mode the graph itself is org-scoped so
    cross-org collision is structurally impossible.
    """
    return f"meeting_ops_meeting_{session_pk}"


async def delete_session_from_brigade(org_id: int, node_name: str) -> BrigadeWriteResult:
    """Best-effort cascading erasure for one Meeting-Ops graph node."""
    target = _resolve_tenancy(org_id, strict=True)
    client = BrigadeClient()
    try:
        return await client.delete_entity_subgraph(name=node_name, agent_id=target.agent_id)
    finally:
        await client.aclose()


def _speaker_node_name(speaker_id: int) -> str:
    return f"meeting_ops_speaker_{speaker_id}"


def _action_item_node_name(action_item_id: int) -> str:
    return f"meeting_ops_action_{action_item_id}"


def _topic_node_name(session_pk: int, idx: int) -> str:
    # Topics + decisions don't have stable Postgres IDs today (they
    # live as JSON inside final_summary). We derive a stable name from
    # (session, position) so re-runs of the writer dedupe. A later
    # phase moves these to first-class tables (per design doc Layer 2).
    return f"meeting_ops_topic_{session_pk}_{idx}"


def _decision_node_name(session_pk: int, idx: int) -> str:
    return f"meeting_ops_decision_{session_pk}_{idx}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _extract_topics(final_summary: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull topic strings out of the summary JSON.

    Today the summarizer emits ``bullets`` (one per topic / discussion
    point) as the closest field; ``sections.topics`` is also populated
    by `services.auto_summarization_service._parse_markdown_sections`.
    Either source is acceptable; we prefer ``bullets`` because it's
    consistently structured. Falls back to ``sections.topics`` for
    legacy sessions.

    Returns a list of {"text": str, "importance": float?} — importance
    is None today; future LLM passes can populate it for the graph viz.
    """
    if not final_summary:
        return []
    bullets = final_summary.get("bullets") or []
    topics: list[dict[str, Any]] = []
    if isinstance(bullets, list):
        for b in bullets:
            text = (b if isinstance(b, str) else str(b or "")).strip()
            if text:
                topics.append({"text": text, "importance": None})
    if not topics:
        sections = final_summary.get("sections") or {}
        raw = sections.get("topics") if isinstance(sections, dict) else None
        if isinstance(raw, str):
            for line in raw.split("\n"):
                line = line.strip(" -*\t")
                if line:
                    topics.append({"text": line, "importance": None})
    return topics


def _extract_decisions(final_summary: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull decision strings + best-effort attribution out of the
    summary JSON.

    The summarizer prompt asks for ``decisions`` as a list of strings
    shaped "<text> (decided by Speaker)" — we keep the original verbatim
    text and try to parse the parenthetical for the speaker name. The
    speaker name is then matched against the session's speakers by the
    writer at edge time.
    """
    if not final_summary:
        return []
    raw = final_summary.get("decisions") or []
    decisions: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return []
    for d in raw:
        text = (d if isinstance(d, str) else str(d or "")).strip()
        if not text:
            continue
        # "...text... (decided by Alice)" pattern.
        decided_by: Optional[str] = None
        if "(decided by " in text.lower():
            try:
                tail = text[text.lower().rindex("(decided by ") :]
                name_chunk = tail.partition("(decided by ")[2].rstrip(")").strip()
                if name_chunk:
                    decided_by = name_chunk
            except Exception:  # noqa: BLE001
                decided_by = None
        decisions.append({"text": text, "decided_by": decided_by})
    return decisions


def _build_meeting_properties(session: Any, org_id: int) -> dict[str, Any]:
    """Property dict for the :Meeting node. Excludes large text bodies
    (transcript, full summary) — those stay in Postgres + Qdrant and
    aren't useful as graph properties anyway."""
    return {
        "id": session.id,
        "session_uuid": session.session_id,
        "title": session.title or session.name or f"Meeting {session.id}",
        "started_at": _iso(session.started_at),
        "ended_at": _iso(session.ended_at),
        "duration_sec": float(session.duration or 0),
        "meeting_type": session.meeting_type,
        "source_type": session.source_type,
        "status": session.status,
        "org_id": org_id,
    }


def _build_speaker_properties(speaker: Any, org_id: int) -> dict[str, Any]:
    return {
        "id": speaker.id,
        "display_name": speaker.display_name,
        "email": speaker.email,
        "company": speaker.company,
        "has_voice_sample": bool(getattr(speaker, "centroid_embedding", None)),
        "org_id": org_id,
    }


def _build_action_item_properties(
    item: Any, org_id: int, owner_speaker_id: Optional[int]
) -> dict[str, Any]:
    return {
        "id": item.id,
        "text": item.text,
        "owner": item.owner,
        "owner_speaker_id": owner_speaker_id,
        "status": item.status,
        "due_date": _iso(item.due_date) if item.due_date else None,
        "source_meeting_id": item.session_id,
        "org_id": org_id,
    }


def _match_owner_to_speaker(
    owner: Optional[str], speakers: list[Any]
) -> Optional[int]:
    """Best-effort name match. Action items store ``owner`` as free text
    ("Aaron", "Mike at finance") — we match against the session's
    SpeakerProfile display_name with a case-insensitive prefix check.
    Returns None when no confident match exists; the writer still
    creates the node without the ASSIGNED_TO edge in that case."""
    if not owner:
        return None
    needle = owner.strip().lower()
    if not needle:
        return None
    # Exact match wins.
    for s in speakers:
        dn = (s.display_name or "").strip().lower()
        if dn and dn == needle:
            return s.id
    # First-token prefix (handles "Aaron" matching "Aaron Stransky").
    first_token = needle.split()[0] if needle else ""
    if first_token:
        for s in speakers:
            dn = (s.display_name or "").strip().lower()
            if dn and dn.startswith(first_token):
                return s.id
    return None


def _stamp_session_synced(
    db: Session, session: Any, node_name: str
) -> None:
    """Mark the session as synced (timestamp + node_name) in Postgres.

    Wrapped in its own try/except by the caller so a stamp failure
    never raises into the writer's pipeline; we'd rather the session
    look "unsynced" and get re-written by the nightly job than blow
    up the recording's background task.
    """
    session.brigade_graph_node_id = node_name
    session.brigade_synced_at = datetime.now(timezone.utc)
    db.commit()


def _set_sync_state(
    db: Session,
    session: Any,
    status: str,
    *,
    error: Optional[str] = None,
    increment_attempt: bool = False,
) -> None:
    """Persist graph projection state without making it part of meeting success."""
    session.brigade_sync_status = status
    if increment_attempt:
        session.brigade_sync_attempt_count = (session.brigade_sync_attempt_count or 0) + 1
        session.brigade_sync_attempted_at = datetime.now(timezone.utc)
    session.brigade_sync_error = error[:500] if error else None
    if status == "disabled":
        # Do not leave a stale graph read link around after an org opts out or
        # the deployment removes the Brigade configuration.
        session.brigade_graph_node_id = None
        session.brigade_synced_at = None
    db.commit()


async def write_meeting_to_brigade(
    session_id: int,
    db: Session,
    *,
    client: Optional[BrigadeClient] = None,
    completion_mode: str = "reprocess",
) -> BrigadeWriteResult:
    """Orchestrate the per-session graph write.

    Args:
        session_id: ``recording_sessions.id`` (integer PK).
        db: SQLAlchemy session (caller-provided; we don't open our own
            so the call site controls transaction scope).
        client: Optional BrigadeClient; tests inject a stub. When
            None we build a default-config client and close it on exit.
        completion_mode: ``"reprocess"`` (full reprocess pipeline) or
            ``"live"`` (browser/live finalize without reprocess).
            Stored on the Meeting node for analytics.

    Returns:
        BrigadeWriteResult with ok=True for both real writes and the
        no-op path (no API key set). ok=False only for partial-write
        failures the caller may want to log; the writer never raises.
    """
    # Lazy import keeps the brigade_client module a hard dependency only
    # when this is actually called (most code paths in tests never touch
    # the writer).
    from database.models import ActionItem, RecordingSession, SpeakerProfile, SpeakerSessionLink

    session: Any = None
    try:
        session = (
            db.query(RecordingSession)
            .filter(RecordingSession.id == session_id)
            .first()
        )
        if session is None:
            logger.info(
                "brigade_writer skipped: session_id=%s not found", session_id
            )
            return BrigadeWriteResult(
                ok=False, mode="error", detail="session not found"
            )

        # The meeting can complete while this is writing; the durable status
        # gives the UI and the retry endpoint an honest state either way.
        _set_sync_state(db, session, "syncing", increment_attempt=True)

        org_id = session.organization_id
        if org_id is None:
            # Should never happen (org_id is NOT NULL on Postgres) but
            # defend against test fixtures that may seed differently.
            logger.warning(
                "brigade_writer skipped: session_id=%s has no organization_id",
                session_id,
            )
            return BrigadeWriteResult(
                ok=False, mode="error", detail="no organization_id"
            )

        # v3.19.0: honor the per-org integration toggle. When the org
        # has integrations.brigade.enabled=False we no-op (returns
        # ok=True, mode="noop") regardless of env-var creds, so an
        # opted-out org never leaks data into a shared Brigade.
        from services.integrations.org_config import resolve_brigade

        brigade_cfg = resolve_brigade(db, org_id)
        if not brigade_cfg.enabled and brigade_cfg.source == "disabled":
            logger.info(
                "brigade_writer org=%s explicitly disabled brigade integration "
                "— skipping write for session=%s",
                org_id,
                session_id,
            )
            _set_sync_state(db, session, "disabled")
            return BrigadeWriteResult(ok=True, mode="noop", detail="Brigade integration disabled")

        target = _resolve_tenancy(
            org_id,
            tenancy_mode_override=brigade_cfg.extra.get("tenancy_mode")
            if brigade_cfg.source == "org_override"
            else None,
            shared_agent_id_override=brigade_cfg.extra.get("shared_agent_id")
            if brigade_cfg.source == "org_override"
            else None,
            # Fail closed on a typo'd mode — never silently commingle orgs
            # into the shared graph (the top-level except logs + skips).
            strict=True,
        )
        owned_client = client is None
        # If the org has its own creds/URL, instantiate the client with
        # them. Tests still inject their own client via ``client=`` so
        # this branch only runs in production.
        if client is None:
            if brigade_cfg.source == "org_override":
                active_client = BrigadeClient(
                    base_url=brigade_cfg.api_base_url,
                    api_key=brigade_cfg.api_key,
                )
            else:
                active_client = BrigadeClient()
        else:
            active_client = client

        try:
            result = await _write_all(
                db=db,
                session=session,
                org_id=org_id,
                target=target,
                client=active_client,
                completion_mode=completion_mode,
            )
            if result.mode == "noop":
                _set_sync_state(db, session, "disabled")
            elif result.ok:
                _set_sync_state(db, session, "synced")
            else:
                _set_sync_state(db, session, "failed", error=result.detail or "Graph write failed")
            return result
        finally:
            if owned_client:
                await active_client.aclose()
    except Exception as exc:  # noqa: BLE001
        # Top-level catch — the writer must never raise into the
        # recording pipeline. Any unexpected error is logged with full
        # traceback so the operator can diagnose.
        logger.exception(
            "brigade_writer write_meeting_to_brigade swallowed error session_id=%s: %s",
            session_id,
            exc,
        )
        if session is not None:
            try:
                _set_sync_state(db, session, "failed", error=str(exc))
            except Exception:  # noqa: BLE001
                logger.exception("brigade_writer could not persist failed state session_id=%s", session_id)
        return BrigadeWriteResult(
            ok=False, mode="error", detail=str(exc)[:200]
        )


async def _write_all(
    *,
    db: Session,
    session: Any,
    org_id: int,
    target: _GraphTarget,
    client: BrigadeClient,
    completion_mode: str = "reprocess",
) -> BrigadeWriteResult:
    """Inner orchestrator. Splits out from the top-level so the catch
    in ``write_meeting_to_brigade`` stays simple."""
    from database.models import ActionItem, SpeakerProfile, SpeakerSessionLink

    # ------------------------------------------------------------------
    # 1. Meeting node
    # ------------------------------------------------------------------
    meeting_name = _meeting_node_name(session.id)
    meeting_props = _build_meeting_properties(session, org_id)
    meeting_props["graph_name"] = target.graph_name
    meeting_props["tenancy_mode"] = target.mode
    meeting_props["completion_mode"] = completion_mode

    results: list[BrigadeWriteResult] = []

    async def _write(operation: Any) -> BrigadeWriteResult:
        result = await operation
        results.append(result)
        return result

    meeting_res = await _write(client.upsert_entity(
        name=meeting_name,
        entity_type=ENTITY_TYPE_MEETING,
        properties=meeting_props,
        agent_id=target.agent_id,
    ))
    # If the Meeting write fails, we still attempt children so a partial
    # graph is better than nothing. The reconciliation job will pick up
    # the gap by ``brigade_synced_at IS NULL``.
    if not meeting_res.ok and meeting_res.mode != "noop":
        logger.warning(
            "brigade_writer meeting upsert failed session_id=%s detail=%s",
            session.id,
            meeting_res.detail,
        )

    # ------------------------------------------------------------------
    # 2. Speakers — pulled via SpeakerSessionLink so we only push
    #    speakers that actually showed up in this session.
    # ------------------------------------------------------------------
    speaker_links = (
        db.query(SpeakerSessionLink)
        .filter(SpeakerSessionLink.session_id == session.id)
        .filter(SpeakerSessionLink.speaker_id.isnot(None))
        .all()
    )
    speaker_profiles: list[Any] = []
    seen_speaker_ids: set[int] = set()
    if speaker_links:
        speaker_ids = [link.speaker_id for link in speaker_links]
        speaker_profiles = (
            db.query(SpeakerProfile)
            .filter(SpeakerProfile.id.in_(speaker_ids))
            .all()
        )

    for speaker in speaker_profiles:
        if speaker.id in seen_speaker_ids:
            continue
        seen_speaker_ids.add(speaker.id)
        speaker_name = _speaker_node_name(speaker.id)
        await _write(client.upsert_entity(
            name=speaker_name,
            entity_type=ENTITY_TYPE_SPEAKER,
            properties=_build_speaker_properties(speaker, org_id),
            agent_id=target.agent_id,
        ))
        await _write(client.upsert_edge(
            from_name=meeting_name,
            from_type=ENTITY_TYPE_MEETING,
            relationship=REL_HAS_SPEAKER,
            to_name=speaker_name,
            to_type=ENTITY_TYPE_SPEAKER,
            agent_id=target.agent_id,
        ))

    # ------------------------------------------------------------------
    # 3. Action items
    # ------------------------------------------------------------------
    action_items = (
        db.query(ActionItem)
        .filter(ActionItem.session_id == session.id)
        .order_by(ActionItem.sort_order.asc(), ActionItem.id.asc())
        .all()
    )
    for item in action_items:
        owner_speaker_id = _match_owner_to_speaker(item.owner, speaker_profiles)
        action_name = _action_item_node_name(item.id)
        await _write(client.upsert_entity(
            name=action_name,
            entity_type=ENTITY_TYPE_ACTION_ITEM,
            properties=_build_action_item_properties(
                item, org_id, owner_speaker_id
            ),
            agent_id=target.agent_id,
        ))
        await _write(client.upsert_edge(
            from_name=meeting_name,
            from_type=ENTITY_TYPE_MEETING,
            relationship=REL_HAS_ACTION_ITEM,
            to_name=action_name,
            to_type=ENTITY_TYPE_ACTION_ITEM,
            agent_id=target.agent_id,
        ))
        if owner_speaker_id is not None:
            await _write(client.upsert_edge(
                from_name=action_name,
                from_type=ENTITY_TYPE_ACTION_ITEM,
                relationship=REL_ASSIGNED_TO,
                to_name=_speaker_node_name(owner_speaker_id),
                to_type=ENTITY_TYPE_SPEAKER,
                agent_id=target.agent_id,
            ))

    # ------------------------------------------------------------------
    # 4. Topics (LLM-derived from final_summary.bullets)
    # ------------------------------------------------------------------
    summary = session.final_summary or {}
    topics = _extract_topics(summary)
    for idx, topic in enumerate(topics):
        topic_name = _topic_node_name(session.id, idx)
        await _write(client.upsert_entity(
            name=topic_name,
            entity_type=ENTITY_TYPE_TOPIC,
            properties={
                "text": topic["text"],
                "importance": topic.get("importance"),
                "source_meeting_id": session.id,
                "org_id": org_id,
            },
            agent_id=target.agent_id,
        ))
        await _write(client.upsert_edge(
            from_name=meeting_name,
            from_type=ENTITY_TYPE_MEETING,
            relationship=REL_HAS_TOPIC,
            to_name=topic_name,
            to_type=ENTITY_TYPE_TOPIC,
            agent_id=target.agent_id,
        ))

    # ------------------------------------------------------------------
    # 5. Decisions (LLM-derived from final_summary.decisions)
    # ------------------------------------------------------------------
    decisions = _extract_decisions(summary)
    for idx, decision in enumerate(decisions):
        decision_name = _decision_node_name(session.id, idx)
        decided_by_speaker_id = _match_owner_to_speaker(
            decision.get("decided_by"), speaker_profiles
        )
        await _write(client.upsert_entity(
            name=decision_name,
            entity_type=ENTITY_TYPE_DECISION,
            properties={
                "text": decision["text"],
                "decided_by": decision.get("decided_by"),
                "decided_by_speaker_id": decided_by_speaker_id,
                "source_meeting_id": session.id,
                "org_id": org_id,
            },
            agent_id=target.agent_id,
        ))
        await _write(client.upsert_edge(
            from_name=meeting_name,
            from_type=ENTITY_TYPE_MEETING,
            relationship=REL_HAS_DECISION,
            to_name=decision_name,
            to_type=ENTITY_TYPE_DECISION,
            agent_id=target.agent_id,
        ))
        if decided_by_speaker_id is not None:
            await _write(client.upsert_edge(
                from_name=decision_name,
                from_type=ENTITY_TYPE_DECISION,
                relationship=REL_DECIDED_BY,
                to_name=_speaker_node_name(decided_by_speaker_id),
                to_type=ENTITY_TYPE_SPEAKER,
                agent_id=target.agent_id,
            ))

    # ------------------------------------------------------------------
    # 6. Stamp the session as synced. Best-effort — failure here doesn't
    #    invalidate the graph writes; the next reconciliation pass will
    #    re-stamp (the writes themselves are MERGE-idempotent).
    # ------------------------------------------------------------------
    failures = [result for result in results if not result.ok]
    if failures:
        detail = next((result.detail for result in failures if result.detail), "One or more graph writes failed")
        logger.warning("brigade_writer partial write session_id=%s failures=%d detail=%s", session.id, len(failures), detail)
        return BrigadeWriteResult(ok=False, mode="error", detail=detail)

    # No-op is a configuration state, not a confirmed graph sync. Do not
    # retain a stale graph link when an operator disables the integration.
    if meeting_res.mode == "noop":
        return BrigadeWriteResult(ok=True, mode="noop", detail="Brigade API is not configured")

    try:
        _stamp_session_synced(db, session, meeting_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("brigade_writer stamp_synced failed session_id=%s: %s", session.id, exc)
        return BrigadeWriteResult(ok=False, mode="error", detail=f"graph write succeeded but status stamp failed: {exc}")

    return BrigadeWriteResult(
        ok=True,
        mode=meeting_res.mode,
        detail=f"graph={target.graph_name} mode={target.mode}",
    )


__all__ = [
    "write_meeting_to_brigade",
    "delete_session_from_brigade",
    "TENANCY_SHARED",
    "TENANCY_PER_ORG_GRAPH",
    "TENANCY_PER_ORG_INSTANCE",
    "DEFAULT_TENANCY_MODE",
    "COMPLETION_MODE_REPROCESS",
    "COMPLETION_MODE_LIVE",
]
