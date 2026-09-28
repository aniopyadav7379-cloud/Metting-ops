"""Best-effort task writer: Meeting-Ops action items -> Project-Ops tasks.

Public surface:

  await write_action_items_to_projectops(
      db=db, session_pk=session_pk, completion_mode="reprocess"
  )

Called from two paths (mirroring ``services.brigade_writer``):

  * ``api.recording._run_session_reprocess`` (completion_mode="reprocess")
    after the Brigade graph write block.
  * the live/browser-only finalize background task in ``api.recording``
    (completion_mode="live").

What this IS: a one-way write from Meeting-Ops -> Project-Ops on meeting
completion. When a meeting's ``action_items`` are (re)written, we create a
matching Project-Ops task per item on a per-org target project.

What this ISN'T: bidirectional sync. We don't poll PO. We don't roll PO
tasks back on meeting delete (PO owns its own delete UX). Source of truth
for the *meeting side* is ``action_items``; for the *task side* it's PO.

Idempotency: legacy direct links remain readable from ``raw_payload``. The
propose/review lifecycle uses explicit ActionItem linkage columns.

Failure posture: the writer NEVER raises into the recording pipeline.
Every exception is logged + swallowed (top-level catch). The legacy direct-task
writer is a no-op without its own Project-Ops credential. The triage writer
instead requires a workspace-bound Brigade token and fails closed when that
exchange is unavailable; it does not require a second Project-Ops credential.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session


logger = logging.getLogger(__name__)


# raw_payload stamp keys (the idempotency marker + provenance).
STAMP_TASK_ID = "po_task_id"
STAMP_PROJECT_NUMBER = "po_project_number"
STAMP_SYNCED_AT = "po_synced_at"

# Triage-path stamp keys. Set when an action item has been submitted to the
# Project-Ops triage inbox (propose-only) — distinct from STAMP_TASK_ID
# because no task exists until a human approves in PO. The idempotency
# marker for the triage submit path.
STAMP_TRIAGE_SUBMITTED_AT = "po_triage_submitted_at"
STAMP_TRIAGE_SOURCE_TYPE = "po_triage_source_type"

# The sourceType Meeting-Ops stamps on candidates submitted to PO triage.
# PO dedups on (sourceType, sourceActionItemId), so this must be stable.
TRIAGE_SOURCE_TYPE = "MEETING_OPS"

# Org-scoped setting key, stored inside Organization.settings (JSONB).
ORG_DEFAULT_SETTING_KEY = "projectops_default_project_number"

# Per-meeting override key inside RecordingSession.processing_metadata.
SESSION_OVERRIDE_KEY = "po_project_override"

# Project-app marker that means session.project_id is a Project-Ops UUID.
PROJECT_APP_PROJECTOPS = "project-ops"

# Defaults for created tasks (per spec).
DEFAULT_TASK_PRIORITY = "MEDIUM"
DEFAULT_TASK_TYPE = "OTHER"

@dataclass(frozen=True)
class ProjectOpsWriteResult:
    """Lightweight return value for the writer's call sites.

    ``ok`` is True for real writes AND the no-op / no-target / gated-off
    paths (the meeting completed; that's the user-facing contract).
    ``mode`` lets callers + tests distinguish: "live" (real), "no-op" (PO
    unconfigured or org-disabled), "gated-off" (auto-push opt-in is OFF),
    "no-target" (no project resolved), "error".
    """

    ok: bool
    mode: str  # "live" | "no-op" | "gated-off" | "no-target" | "error"
    created: int = 0
    skipped: int = 0
    detail: Optional[str] = None


def _utcnow_iso() -> str:
    """ISO-8601 UTC with a trailing Z (matches the stamp shape in the
    integration spec, e.g. ``2026-05-28T16:30:00Z``)."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def get_org_default_project_number(
    db: Session, organization_id: int
) -> Optional[str]:
    """Read ``projectops_default_project_number`` from Organization.settings.

    Stored on the existing ``Organization.settings`` JSONB column (org-scoped,
    no migration). Returns None when unset / org missing.
    """
    from auth.models import Organization

    org = (
        db.query(Organization)
        .filter(Organization.id == organization_id)
        .first()
    )
    if org is None:
        return None
    settings = org.settings or {}
    value = settings.get(ORG_DEFAULT_SETTING_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def set_org_default_project_number(
    db: Session, organization_id: int, project_number: Optional[str]
) -> bool:
    """Set (or clear, with None) the org default target project.

    Reassigns ``Organization.settings`` (rather than mutating in place) so
    SQLAlchemy reliably detects the JSONB change. Commits. Returns True on
    success, False if the org doesn't exist. The v1 wiring path — call from
    the backfill script's ``--set-default-project`` flag or a shell.
    """
    from auth.models import Organization

    org = (
        db.query(Organization)
        .filter(Organization.id == organization_id)
        .first()
    )
    if org is None:
        return False
    settings = dict(org.settings or {})
    if project_number:
        settings[ORG_DEFAULT_SETTING_KEY] = project_number.strip()
    else:
        settings.pop(ORG_DEFAULT_SETTING_KEY, None)
    org.settings = settings
    db.commit()
    return True


def _resolve_target_project(
    db: Session, session: Any
) -> tuple[Optional[str], str]:
    """Resolve the Project-Ops target for this session's action items.

    Resolution order (most-specific first):
      0. The existing session->PO link: ``session.project_app ==
         'project-ops'`` + ``session.project_id`` (a PO project UUID).
         This is the canonical link the rest of Meeting-Ops already uses;
         honoring it means a session explicitly tied to a PO project just
         works, and we skip number->UUID resolution entirely.
      1. Per-meeting override: ``session.processing_metadata[po_project_override]``
         (a project number or UUID). Backend-only in v1; UI deferred.
      2. Org default: ``Organization.settings[projectops_default_project_number]``.
      3. None -> caller skips (mode="no-target").

    Returns ``(project_ref, source)`` where project_ref is a number
    ("P-00055") or a UUID, or ``(None, "none")``.
    """
    # 0. Existing canonical session -> PO project link.
    if (
        getattr(session, "project_app", None) == PROJECT_APP_PROJECTOPS
        and getattr(session, "project_id", None)
    ):
        return str(session.project_id).strip(), "session_link"

    # 1. Per-meeting override.
    meta = session.processing_metadata or {}
    if isinstance(meta, dict):
        override = meta.get(SESSION_OVERRIDE_KEY)
        if isinstance(override, str) and override.strip():
            return override.strip(), "override"

    # 2. Org default.
    org_default = get_org_default_project_number(db, session.organization_id)
    if org_default:
        return org_default, "org_default"

    # 3. Nothing.
    return None, "none"


def _meeting_label(session: Any) -> str:
    return (
        session.title
        or session.name
        or f"Meeting {session.id}"
    )


def _meeting_date(session: Any) -> str:
    """Best-effort human date for the task description."""
    for attr in ("meeting_date", "started_at", "created_at"):
        value = getattr(session, attr, None)
        if value is not None:
            try:
                return value.date().isoformat()
            except AttributeError:
                # ``meeting_date`` is already a date.
                return str(value)
    return "unknown date"


def _build_description(session: Any, item: Any) -> str:
    owner = (item.owner or "").strip() or "unassigned"
    return (
        f"From meeting: {_meeting_label(session)}, "
        f"{_meeting_date(session)}, owner: {owner}"
    )


def _stamp_item(item: Any, *, task_id: str, project_number: Optional[str]) -> None:
    """Write the PO link onto the action item's raw_payload. Reassigns the
    dict (not in-place mutation) so SQLAlchemy flags the JSONB column dirty
    without needing flag_modified."""
    payload = dict(item.raw_payload or {})
    payload[STAMP_TASK_ID] = task_id
    if project_number:
        payload[STAMP_PROJECT_NUMBER] = project_number
    payload[STAMP_SYNCED_AT] = _utcnow_iso()
    item.raw_payload = payload
    item.project_ops_link_state = "approved_linked"
    item.project_ops_task_id = task_id
    item.project_ops_project_number = project_number
    item.project_ops_last_sync_attempt_at = datetime.now(timezone.utc)
    item.project_ops_last_synced_at = datetime.now(timezone.utc)
    item.project_ops_sync_error = None


def _stamp_triage(item: Any, proposal_id: Optional[str] = None) -> None:
    """Mark an action item as submitted to the Project-Ops triage inbox.
    The idempotency marker for the triage path (mirrors ``_stamp_item`` but
    carries no task id — no task exists until a human approves in PO).
    Reassigns the dict so SQLAlchemy flags the JSONB column dirty."""
    from services.projectops_lifecycle import mark_proposed

    mark_proposed(item, proposal_id)


# Session-level marker keys (written to RecordingSession.processing_metadata)
# so a failed Project-Ops push is visible + retryable. Distinct from the
# per-item raw_payload stamps: those gate idempotency; this records the
# health of the LAST push attempt for the whole session.
STAMP_SESSION_PUSH_FAILED_AT = "po_push_failed_at"
STAMP_SESSION_PUSH_ERROR = "po_push_error"
STAMP_SESSION_PUSH_MODE = "po_push_mode"


def _stamp_session_push_failure(
    db: Session, session: Any, *, mode: str, detail: Optional[str]
) -> None:
    """Record a Project-Ops push failure on the session so it's queryable +
    retryable (a backfill can target sessions carrying po_push_failed_at).
    Best-effort: a stamp failure must never mask the original error, so this
    swallows + logs. Reassigns processing_metadata so SQLAlchemy flags the
    JSON column dirty."""
    try:
        meta = dict(session.processing_metadata or {})
        meta[STAMP_SESSION_PUSH_FAILED_AT] = _utcnow_iso()
        meta[STAMP_SESSION_PUSH_ERROR] = (detail or "")[:200]
        meta[STAMP_SESSION_PUSH_MODE] = mode
        session.processing_metadata = meta
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "projectops_writer could not stamp push failure on session=%s: %s",
            getattr(session, "id", "?"),
            exc,
        )


def _clear_session_push_failure(db: Session, session: Any) -> None:
    """Clear a prior push-failure marker after a successful push so the
    session self-heals and isn't re-picked by a failure-targeted backfill.
    No-op when nothing was stamped. Best-effort."""
    try:
        meta = session.processing_metadata or {}
        if not isinstance(meta, dict) or STAMP_SESSION_PUSH_FAILED_AT not in meta:
            return
        meta = dict(meta)
        meta.pop(STAMP_SESSION_PUSH_FAILED_AT, None)
        meta.pop(STAMP_SESSION_PUSH_ERROR, None)
        meta.pop(STAMP_SESSION_PUSH_MODE, None)
        session.processing_metadata = meta
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "projectops_writer could not clear push-failure stamp session=%s: %s",
            getattr(session, "id", "?"),
            exc,
        )


def _explicit_project_ref(db: Session, session: Any) -> Optional[str]:
    """The session's EXPLICIT Project-Ops link — the session->PO link
    (``project_app == 'project-ops'`` + ``project_id``) or a per-meeting
    override — if any. Unlike ``_resolve_target_project`` this does NOT fall
    back to the org default: it's only a routing HINT for triage, and the
    org default is too weak a signal to bias the agent's routing. Returns a
    project number or UUID, or None."""
    if (
        getattr(session, "project_app", None) == PROJECT_APP_PROJECTOPS
        and getattr(session, "project_id", None)
    ):
        return str(session.project_id).strip()
    meta = session.processing_metadata or {}
    if isinstance(meta, dict):
        override = meta.get(SESSION_OVERRIDE_KEY)
        if isinstance(override, str) and override.strip():
            return override.strip()
    return None


def _candidate_source_ref(
    session: Any, item: Any, linked_ref: Optional[str]
) -> dict[str, Any]:
    """Provenance + routing hint for a triage candidate's ``sourceRef``.
    Carries enough for the PO reviewer (and a future hint-aware router) to
    see where the item came from and any explicitly intended project."""
    ref: dict[str, Any] = {
        "app": "meeting-ops",
        "meetingTitle": _meeting_label(session),
        "meetingDate": _meeting_date(session),
        "sessionId": session.id,
        "sessionUid": getattr(session, "session_id", None),
        "actionItemId": item.id,
    }
    owner = (item.owner or "").strip()
    if owner:
        ref["owner"] = owner
    if linked_ref:
        ref["linkedProjectRef"] = linked_ref
    return ref


async def write_action_items_to_projectops(
    *,
    db: Session,
    session_pk: int,
    completion_mode: str = "reprocess",
    client: Optional[Any] = None,
) -> ProjectOpsWriteResult:
    """Create Project-Ops tasks for this session's un-synced action items.

    Args:
        db: caller-provided SQLAlchemy session (we don't open our own so
            the call site owns transaction scope).
        session_pk: ``recording_sessions.id`` (integer PK).
        completion_mode: "reprocess" or "live" — logged for provenance.
        client: optional ProjectOpsClient (tests inject a stub). When None
            a default-config client is built + closed on exit.

    Returns ProjectOpsWriteResult. Never raises.
    """
    # Lazy imports keep projectops_client / models off the import path
    # until this is actually called.
    from services.projectops_client import ProjectOpsClient, ProjectOpsClientError

    try:
        from database.models import ActionItem, RecordingSession

        session = (
            db.query(RecordingSession)
            .filter(RecordingSession.id == session_pk)
            .first()
        )
        if session is None:
            logger.info(
                "projectops_writer skipped: session_pk=%s not found", session_pk
            )
            return ProjectOpsWriteResult(
                ok=False, mode="error", detail="session not found"
            )

        # v3.19.0: honor the per-org integration toggle. When the org
        # has integrations.project_ops.enabled=False, no-op even if
        # PROJECTOPS_API_KEY is set in env — no leakage.
        from services.integrations.org_config import resolve_project_ops

        po_cfg = resolve_project_ops(db, session.organization_id)
        if not po_cfg.enabled and po_cfg.source == "disabled":
            logger.info(
                "projectops_writer org=%s explicitly disabled project_ops "
                "integration — skipping session=%s",
                session.organization_id,
                session_pk,
            )
            return ProjectOpsWriteResult(ok=True, mode="no-op")

        # Auto-push gate (v3.26.0): the automatic outbound push of
        # extracted action items to Project-Ops is DELIBERATE, not
        # automatic. Not everything the model flags is a real task, so a
        # blanket auto-push creates task noise. The push only fires when
        # it's been opted in — per-org via
        # integrations.project_ops.auto_push_action_items, or (for orgs on
        # the env-default path) via the PROJECTOPS_AUTO_PUSH_ACTION_ITEMS
        # deploy flag. Both default OFF. This gate ONLY governs the
        # automatic push driven by the recording/finalize pipeline; action
        # item extraction, storage, and display are untouched. Project-Ops
        # Task status is never written from this Meeting-Ops path.
        if not po_cfg.extra.get("auto_push_action_items", False):
            logger.info(
                "projectops_writer org=%s auto-push disabled (opt-in OFF, "
                "source=%s) — skipping automatic push session=%s mode=%s",
                session.organization_id,
                po_cfg.source,
                session_pk,
                completion_mode,
            )
            return ProjectOpsWriteResult(ok=True, mode="gated-off")

        target_ref, source = _resolve_target_project(db, session)
        # If the session+meta didn't yield a target but the per-org
        # integration config has a default_project_number, use that.
        # (resolve_project_ops merges legacy Organization.settings into
        # extra["default_project_number"] so this also handles pre-v3.19
        # orgs.)
        if not target_ref:
            org_default = po_cfg.extra.get("default_project_number")
            if org_default:
                target_ref, source = org_default, "org_default"
        if not target_ref:
            logger.info(
                "projectops_writer no target project for session=%s "
                "(no session link, override, or org default) — skipping",
                session_pk,
            )
            return ProjectOpsWriteResult(ok=True, mode="no-target")

        owned_client = client is None
        # Per-org override wins when source==org_override; otherwise the
        # client falls back to env vars (legacy path).
        if client is None:
            if po_cfg.source == "org_override":
                active_client = ProjectOpsClient(
                    base_url=po_cfg.api_base_url,
                    api_key=po_cfg.api_key,
                )
            else:
                active_client = ProjectOpsClient()
        else:
            active_client = client
        try:
            if not active_client.is_live:
                logger.info(
                    "projectops_writer: PO unconfigured (PROJECTOPS_API_KEY "
                    "unset), skipping session=%s",
                    session_pk,
                )
                return ProjectOpsWriteResult(ok=True, mode="no-op")

            action_items = (
                db.query(ActionItem)
                .filter(ActionItem.session_id == session.id)
                .order_by(ActionItem.sort_order.asc(), ActionItem.id.asc())
                .all()
            )

            created = 0
            skipped = 0
            for item in action_items:
                existing = (item.raw_payload or {}).get(STAMP_TASK_ID)
                if existing:
                    # Already bridged. Don't duplicate. Project-Ops owns task
                    # status; local checkbox changes never update it.
                    skipped += 1
                    continue

                try:
                    result = await active_client.create_task(
                        project_number=target_ref,
                        title=item.text,
                        description=_build_description(session, item),
                        priority=DEFAULT_TASK_PRIORITY,
                        due_date=item.due_date,
                        type=DEFAULT_TASK_TYPE,
                    )
                except ProjectOpsClientError as exc:
                    # A live write failed. Stop hammering a degraded PO;
                    # leave un-synced rows for the backfill job. Rows
                    # touched so far stay committed (best-effort partial).
                    logger.warning(
                        "projectops_writer create_task failed session=%s "
                        "item=%s target=%s: %s",
                        session_pk,
                        item.id,
                        target_ref,
                        exc,
                    )
                    return ProjectOpsWriteResult(
                        ok=False,
                        mode="error",
                        created=created,
                        skipped=skipped,
                        detail=str(exc)[:200],
                    )

                _stamp_item(
                    item,
                    task_id=result["id"],
                    project_number=result.get("project_number") or _ref_as_number(target_ref),
                )
                db.commit()
                created += 1

            logger.info(
                "projectops_writer session=%s mode=%s target=%s(%s) "
                "created=%d skipped=%d",
                session_pk,
                completion_mode,
                target_ref,
                source,
                created,
                skipped,
            )
            return ProjectOpsWriteResult(
                ok=True,
                mode="live",
                created=created,
                skipped=skipped,
                detail=f"target={target_ref} source={source}",
            )
        finally:
            if owned_client:
                await active_client.aclose()
    except Exception as exc:  # noqa: BLE001
        # Top-level catch — the writer must never raise into the recording
        # pipeline. Logged with traceback for the operator.
        logger.exception(
            "projectops_writer swallowed error session_pk=%s: %s",
            session_pk,
            exc,
        )
        return ProjectOpsWriteResult(
            ok=False, mode="error", detail=str(exc)[:200]
        )


async def submit_action_items_to_triage(
    *,
    db: Session,
    session_pk: int,
    completion_mode: str = "reprocess",
    client: Optional[Any] = None,
) -> ProjectOpsWriteResult:
    """Submit this session's un-submitted action items to Project-Ops'
    TRIAGE agent (propose-only) instead of creating tasks directly.

    This is the finalize/reprocess successor to
    ``write_action_items_to_projectops``. Where the direct writer needed a
    pre-resolved target project and created a task per item, triage needs
    NO target: the PO-side agent routes each item to the best-fitting
    project (or files it in the "needs a project" inbox), dedups against
    existing work, and a human approves in Project-Ops before anything
    becomes a task. That dissolves the "auto-push creates task noise"
    problem the auto-push gate was built for — but we keep the SAME per-org
    opt-in gate so an org still chooses whether finalize feeds its triage
    inbox.

    Contract mirrors the direct writer: NEVER raises into the recording
    pipeline; honors the per-org integration enable + the v3.26.0 auto-push
    opt-in; fails closed when workspace federation is unconfigured. Idempotent via a
    ``po_triage_submitted_at`` stamp on ``action_items.raw_payload`` AND
    PO's own ``(sourceType, sourceActionItemId)`` uniqueness — a re-run
    submits only the un-stamped items and PO skips any it has already seen.
    Items already bridged via the direct path (``po_task_id`` set) are left
    alone (not double-filed).

    A bounded reconciliation job reads approval/task links back after review.
    Project-Ops owns task status; Meeting-Ops local completion never updates it.
    """
    from services.projectops_client import ProjectOpsClient, ProjectOpsClientError

    try:
        from database.models import ActionItem, RecordingSession

        session = (
            db.query(RecordingSession)
            .filter(RecordingSession.id == session_pk)
            .first()
        )
        if session is None:
            logger.info(
                "projectops_triage skipped: session_pk=%s not found", session_pk
            )
            return ProjectOpsWriteResult(
                ok=False, mode="error", detail="session not found"
            )

        # Resolve the meeting org's workspace_id (the uc-registry tenant uuid)
        # so the triage push can carry it via a Brigade-vouched federation
        # token and PO routes the proposals to the correct tenant. Mirrors the
        # recorder-autostamp path in api.recording (load Organization by
        # session.organization_id, read org.workspace_id). Tenant routing is
        # fail-closed: no workspace binding means no cross-app write.
        from auth.models import Organization

        org = (
            db.query(Organization)
            .filter(Organization.id == session.organization_id)
            .first()
        )
        workspace_id = getattr(org, "workspace_id", None) if org else None

        # Per-org integration toggle (same guarantee as the direct writer):
        # an org that turned project_ops OFF never leaks to a shared PO.
        from services.integrations.org_config import resolve_project_ops

        po_cfg = resolve_project_ops(db, session.organization_id)
        if not po_cfg.enabled and po_cfg.source == "disabled":
            logger.info(
                "projectops_triage org=%s explicitly disabled project_ops — "
                "skipping session=%s",
                session.organization_id,
                session_pk,
            )
            return ProjectOpsWriteResult(ok=True, mode="no-op")

        # Same v3.26.0 auto-push opt-in gate as the direct writer: feeding
        # the triage inbox from finalize is opt-in per org (default OFF).
        if not po_cfg.extra.get("auto_push_action_items", False):
            logger.info(
                "projectops_triage org=%s auto-push disabled (opt-in OFF, "
                "source=%s) — skipping session=%s mode=%s",
                session.organization_id,
                po_cfg.source,
                session_pk,
                completion_mode,
            )
            return ProjectOpsWriteResult(ok=True, mode="gated-off")

        owned_client = client is None
        if client is None:
            if po_cfg.source == "org_override":
                active_client = ProjectOpsClient(
                    base_url=po_cfg.api_base_url,
                    api_key=po_cfg.api_key,
                )
            else:
                active_client = ProjectOpsClient()
        else:
            active_client = client
        try:
            action_items = (
                db.query(ActionItem)
                .filter(ActionItem.session_id == session.id)
                .order_by(ActionItem.sort_order.asc(), ActionItem.id.asc())
                .all()
            )

            # Submit only items not already submitted to triage AND not
            # already bridged to a direct PO task — don't double-file work.
            pending = [
                it
                for it in action_items
                if getattr(it, "project_ops_link_state", "local_only")
                in {"local_only", "sync_failed"}
                and not getattr(it, "project_ops_task_id", None)
                and not (it.raw_payload or {}).get(STAMP_TRIAGE_SUBMITTED_AT)
                and not (it.raw_payload or {}).get(STAMP_TASK_ID)
            ]
            already = len(action_items) - len(pending)
            if not pending:
                logger.info(
                    "projectops_triage session=%s nothing new to submit "
                    "(already=%d)",
                    session_pk,
                    already,
                )
                return ProjectOpsWriteResult(
                    ok=True, mode="live", created=0, skipped=already
                )

            # An explicit session<->PO link (or per-meeting override) rides
            # along as a routing hint in each candidate's sourceRef. We omit
            # the org default on purpose — triage should route freely.
            linked_ref = _explicit_project_ref(db, session)
            candidates = [
                {
                    "text": it.text,
                    "owner": (it.owner or None),
                    "sourceActionItemId": str(it.id),
                    "sourceRef": _candidate_source_ref(session, it, linked_ref),
                }
                for it in pending
            ]

            # Obtain a Brigade-exchanged, workspace-bound federation token
            # (aud=project-ops) so Project-Ops routes proposals to the exact
            # tenant. Never fall back to a default service tenant: a delayed
            # proposal is recoverable; a cross-tenant task is not.
            if not workspace_id:
                detail = "Project-Ops sync blocked: workspace is not provisioned"
                logger.error(
                    "projectops_triage session=%s org=%s: %s",
                    session_pk,
                    session.organization_id,
                    detail,
                )
                _stamp_session_push_failure(
                    db, session, mode=completion_mode, detail=detail
                )
                from services.projectops_lifecycle import mark_sync_failed

                for item in pending:
                    mark_sync_failed(item, "WORKSPACE_NOT_PROVISIONED")
                db.commit()
                return ProjectOpsWriteResult(
                    ok=False,
                    mode="error",
                    skipped=already,
                    detail=detail,
                )

            from services.projectops_token import projectops_federation_token

            bearer_override = await projectops_federation_token(workspace_id)
            if (
                not isinstance(bearer_override, str)
                or not bearer_override.strip()
            ):
                detail = (
                    "Project-Ops sync blocked: workspace-bound federation "
                    "token is unavailable"
                )
                logger.error(
                    "projectops_triage session=%s org=%s ws=%s: %s",
                    session_pk,
                    session.organization_id,
                    workspace_id,
                    detail,
                )
                _stamp_session_push_failure(
                    db, session, mode=completion_mode, detail=detail
                )
                from services.projectops_lifecycle import mark_sync_failed

                for item in pending:
                    mark_sync_failed(item, "FEDERATION_TOKEN_UNAVAILABLE")
                db.commit()
                return ProjectOpsWriteResult(
                    ok=False,
                    mode="error",
                    skipped=already,
                    detail=detail,
                )
            bearer_override = bearer_override.strip()

            try:
                result = await active_client.submit_candidate_action_items(
                    candidates,
                    source_type=TRIAGE_SOURCE_TYPE,
                    bearer_override=bearer_override,
                )
            except ProjectOpsClientError as exc:
                # Don't stamp the ITEMS: the un-submitted items get retried on
                # the next finalize/backfill, and PO dedups anything that did
                # land. Eventual consistency, no duplicate proposals. But DO
                # stamp the SESSION so the failed push is visible + a targeted
                # backfill can find it.
                logger.warning(
                    "projectops_triage submit failed session=%s count=%d: %s",
                    session_pk,
                    len(pending),
                    exc,
                )
                _stamp_session_push_failure(
                    db, session, mode=completion_mode, detail=str(exc)
                )
                from services.projectops_lifecycle import mark_sync_failed

                for item in pending:
                    mark_sync_failed(item, "PROJECT_OPS_UNAVAILABLE")
                db.commit()
                return ProjectOpsWriteResult(
                    ok=False,
                    mode="error",
                    skipped=already,
                    detail=str(exc)[:200],
                )

            proposals_by_source = {
                str(proposal.get("sourceActionItemId")): proposal
                for proposal in result.get("proposals", [])
                if isinstance(proposal, dict)
                and proposal.get("sourceActionItemId") is not None
            }
            for it in pending:
                proposal = proposals_by_source.get(str(it.id))
                _stamp_triage(
                    it,
                    proposal.get("id") if proposal else None,
                )
            db.commit()
            _clear_session_push_failure(db, session)

            po_created = (
                int(result.get("created", 0)) if isinstance(result, dict) else 0
            )
            po_skipped = (
                int(result.get("skipped", 0)) if isinstance(result, dict) else 0
            )
            logger.info(
                "projectops_triage session=%s mode=%s submitted=%d "
                "po_created=%d po_skipped=%d already=%d",
                session_pk,
                completion_mode,
                len(pending),
                po_created,
                po_skipped,
                already,
            )
            return ProjectOpsWriteResult(
                ok=True,
                mode="live",
                created=po_created,
                skipped=already + po_skipped,
                detail=f"submitted={len(pending)} po_created={po_created}",
            )
        finally:
            if owned_client:
                await active_client.aclose()
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "projectops_triage swallowed error session_pk=%s: %s",
            session_pk,
            exc,
        )
        return ProjectOpsWriteResult(
            ok=False, mode="error", detail=str(exc)[:200]
        )


def _ref_as_number(project_ref: str) -> Optional[str]:
    """If the resolved target was a human project number (not a UUID),
    use it as the stamped project number when the create response didn't
    echo one back."""
    from services.projectops_client import _looks_like_uuid

    return None if _looks_like_uuid(project_ref) else project_ref


async def _maybe_propagate_status(
    client: Any, item: Any, task_id: str
) -> None:
    """Compatibility no-op: Project-Ops owns Task status."""
    return None


async def propagate_action_item_status(
    item: Any, *, client: Optional[Any] = None
) -> ProjectOpsWriteResult:
    """Compatibility no-op preserving the explicit ownership boundary."""
    return ProjectOpsWriteResult(
        ok=True,
        mode="ownership-boundary",
        detail="Meeting-Ops local completion does not update Project-Ops",
    )


__all__ = [
    "write_action_items_to_projectops",
    "submit_action_items_to_triage",
    "propagate_action_item_status",
    "get_org_default_project_number",
    "set_org_default_project_number",
    "ProjectOpsWriteResult",
    "ORG_DEFAULT_SETTING_KEY",
    "SESSION_OVERRIDE_KEY",
    "TRIAGE_SOURCE_TYPE",
]
