"""Tests for the Project-Ops action-item bridge writer.

Covers the contract for ``services.projectops_writer``:

   1. creates_tasks_and_stamps      — happy path: a session with 3 action
                                       items + an org default project creates
                                       3 PO tasks, stamps po_task_id /
                                       po_project_number / po_synced_at on
                                       each row, returns created=3 skipped=0.
   2. replay_skips_already_synced   — re-running the writer on the same
                                       session creates 0 and skips 3 (the
                                       po_task_id stamp is the idempotency
                                       key); no duplicate tasks.
   3. noop_when_unconfigured        — PROJECTOPS_API_KEY unset => the client
                                       is log-only; the writer returns
                                       ok=True mode="no-op" with no client
                                       calls and no rows stamped.
   4. failure_returns_ok_false      — when create_task raises, the writer
                                       returns ok=False, the rows are left
                                       unchanged, and NO exception propagates.
   5. no_target_when_unresolved     — no session link, no override, and no
                                       org default => mode="no-target",
                                       created=0 skipped=0, no client calls.
   6. session_link_resolves_target  — session.project_app='project-ops' +
                                       project_id (a UUID) is used directly
                                       as the create target.
   7. override_beats_org_default    — processing_metadata.po_project_override
                                       wins over the org default.
   8. done_status_is_local          — replay never sends a Project-Ops status
                                       update; each app owns its own status.
   9. gated_off_when_auto_push       — default posture: with the v3.26.0
      _disabled                        auto-push opt-in gate OFF the writer
                                       returns mode="gated-off", created=0,
                                       and makes zero client calls even when
                                       a target + live client are present.
  10-17. submit_action_items_to_triage — the triage submit path (submit +
                                       stamp, replay-skip, no-target-still-
                                       works, linked-project hint, shared
                                       auto-push gate, eventual-consistency
                                       on failure, no double-file).
  18-20. triage federation token     — the workspace-bound, aud=project-ops
                                       push: uses the Brigade-exchanged token
                                       as bearer_override when the org's
                                       workspace_id resolves, and fails closed
                                       when the binding/token is unavailable.

The PO HTTP client is replaced by an injected stub (``_FakeProjectOpsClient``)
passed via ``write_action_items_to_projectops(client=...)`` — the same
dependency-injection seam the Brigade writer tests use. No network.

The v3.26.0 auto-push opt-in gate is forced ON for the live-path tests by an
autouse fixture (``_enable_auto_push``) that wraps
``services.integrations.org_config.resolve_project_ops``; test 9 re-patches
that seam back OFF to cover the default-disabled state. See
``_patch_auto_push``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
import httpx


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module", autouse=True)
def _writer_schema():
    """Use the database seam directly instead of booting optional NPU routes."""
    from auth import models as auth_models  # noqa: F401
    from database import models_rooms  # noqa: F401
    from database.database import engine
    from database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client():
    """Compatibility fixture: these writer tests do not exercise HTTP."""
    return None


class _FakeProjectOpsClient:
    """Drop-in ProjectOpsClient stub. Records every call; makes no HTTP
    requests. ``create_task`` returns a fixed task id + project number
    unless ``fail`` is set, in which case it raises ProjectOpsClientError
    to exercise the writer's swallow path."""

    def __init__(
        self,
        *,
        is_live: bool = True,
        fail: bool = False,
        task_id: str = "11111111-1111-4111-8111-111111111111",
        project_number: str = "P-00055",
    ) -> None:
        self.is_live = is_live
        self._fail = fail
        self._task_id = task_id
        self._project_number = project_number
        self.created: list[dict[str, Any]] = []
        self.status_updates: list[tuple[str, str]] = []
        self.submitted: list[dict[str, Any]] = []
        self.aclose_called = False

    async def create_task(
        self,
        *,
        project_number: str,
        title: str,
        description: Optional[str] = None,
        priority: str = "MEDIUM",
        due_date: Any = None,
        type: str = "OTHER",
    ) -> dict[str, Any]:
        self.created.append(
            {
                "project_number": project_number,
                "title": title,
                "description": description,
                "priority": priority,
                "due_date": due_date,
                "type": type,
            }
        )
        if self._fail:
            from services.projectops_client import ProjectOpsClientError

            raise ProjectOpsClientError("simulated PO create failure")
        return {"id": self._task_id, "project_number": self._project_number}

    async def update_task_status(self, *, task_id: str, status: str) -> dict[str, Any]:
        self.status_updates.append((task_id, status))
        if self._fail:
            from services.projectops_client import ProjectOpsClientError

            raise ProjectOpsClientError("simulated PO status failure")
        return {}

    async def submit_candidate_action_items(
        self,
        items,
        *,
        source_type: str = "MEETING_OPS",
        timeout_seconds=None,
        bearer_override=None,
    ) -> dict[str, Any]:
        """Records the submitted batch (incl. any per-call bearer_override so
        tests can assert the federation token is threaded through); returns the
        PO triage ingest shape ({created, skipped, proposals}). Raises when
        ``fail`` is set to exercise the writer's eventual-consistency
        (don't-stamp) path."""
        self.submitted.append(
            {
                "items": items,
                "source_type": source_type,
                "bearer_override": bearer_override,
            }
        )
        if self._fail:
            from services.projectops_client import ProjectOpsClientError

            raise ProjectOpsClientError("simulated PO triage submit failure")
        created = len(items)
        return {
            "created": created,
            "skipped": 0,
            "proposals": [{"id": f"prop-{i}"} for i in range(created)],
        }

    async def aclose(self) -> None:
        self.aclose_called = True


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_projectops_env(monkeypatch):
    """Tests must NOT hit a real Project-Ops. Strip every env var that could
    put ``ProjectOpsClient`` into live mode so the suite stays hermetic on a
    deployed host (e.g. meet-backend ships with the PO Keycloak
    client-credentials trio set — without clearing those, a brand-new
    ``ProjectOpsClient()`` resolves is_live=True via the auto-refresh path
    and ``test_noop_when_unconfigured`` flakes). Tests opt into is_live by
    injecting a stub client.

    Also strip ``PROJECTOPS_AUTO_PUSH_ACTION_ITEMS`` so the env-default
    auto-push posture is deterministic; the gate is then driven explicitly
    by ``_enable_auto_push`` / ``_patch_auto_push``."""
    for var in (
        "PROJECTOPS_API_KEY",
        "PROJECTOPS_BASE_URL",
        "PROJECTOPS_API_BASE_URL",
        "PROJECTOPS_KC_TOKEN_URL",
        "PROJECTOPS_KC_CLIENT_ID",
        "PROJECTOPS_KC_CLIENT_SECRET",
        "PROJECTOPS_AUTO_PUSH_ACTION_ITEMS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _patch_auto_push(monkeypatch, *, enabled: bool) -> None:
    """Force the per-org auto-push opt-in gate (v3.26.0) on or off for the
    duration of a test.

    ``write_action_items_to_projectops`` short-circuits to mode="gated-off"
    unless ``resolve_project_ops(...).extra['auto_push_action_items']`` is
    True. The live-path tests in this module configure the *target project*
    via ``Organization.settings`` (legacy field) rather than a full
    ``integrations.project_ops`` block, so they land on the env-default
    resolution branch where auto-push tracks the (default-OFF)
    PROJECTOPS_AUTO_PUSH_ACTION_ITEMS deploy flag — leaving the gate shut.

    We wrap the *real* resolver (preserving its enabled / source /
    disabled / default_project_number semantics, which several tests rely
    on) and only override the one ``extra`` key. The writer imports
    ``resolve_project_ops`` lazily from ``services.integrations.org_config``
    inside the function body, so we must patch it at the source module —
    patching the name on ``services.projectops_writer`` would not be seen.
    """
    import dataclasses

    from services.integrations import org_config

    real = org_config.resolve_project_ops

    def _wrapped(db, organization_id):
        cfg = real(db, organization_id)
        new_extra = dict(cfg.extra)
        new_extra["auto_push_action_items"] = enabled
        return dataclasses.replace(cfg, extra=new_extra)

    monkeypatch.setattr(org_config, "resolve_project_ops", _wrapped)


@pytest.fixture(autouse=True)
def _enable_auto_push(monkeypatch):
    """Open the v3.26.0 auto-push opt-in gate for every test by default so
    the existing live-path contract tests exercise the real write/skip/
    no-op/no-target behavior instead of returning mode="gated-off".

    The dedicated default-OFF coverage test re-patches the same seam back
    to disabled (see ``test_gated_off_when_auto_push_disabled``); because
    monkeypatch applies that override after this fixture's setup and
    restores on teardown, the two compose cleanly."""
    _patch_auto_push(monkeypatch, enabled=True)

    # Live triage tests need the same tenant-bound token shape required in
    # production. Individual federation failure tests override this seam.
    from services import projectops_token as fed
    real_federation_token = fed.projectops_federation_token

    async def _default_federation_token(workspace_id: str):
        return f"test-federation-token-for-{workspace_id}"

    monkeypatch.setattr(
        fed,
        "projectops_federation_token",
        _default_federation_token,
    )
    yield real_federation_token


# asyncio.run() is the supported, version-proof replacement for the
# deprecated get_event_loop()/new_event_loop() dance; the writer/client
# don't cache a loop so a fresh loop per call is fine.
def _run(coro):
    return asyncio.run(coro)


def _seed(
    *,
    org_settings: Optional[dict] = None,
    project_app: Optional[str] = None,
    project_id: Optional[str] = None,
    processing_metadata: Optional[dict] = None,
    items: Optional[list[dict]] = None,
    workspace_id: Optional[str] = "__auto__",
):
    """Create one org + one completed session + N action items.

    ``items`` is a list of dicts (text/owner/status/due_date/raw_payload);
    defaults to 3 simple todo items. ``workspace_id`` sets the org's
    uc-registry tenant uuid (used by the federation-token push path).
    Returns (org_id, session_id)."""
    from auth.models import Organization, User
    from database.database import SessionLocal
    from database.models import ActionItem, RecordingSession

    db = SessionLocal()
    suffix = uuid.uuid4().hex[:6]
    try:
        resolved_workspace_id = (
            f"ws-test-{suffix}" if workspace_id == "__auto__" else workspace_id
        )
        org = Organization(
            name=f"POBridge {suffix}",
            slug=f"pob-{suffix}",
            is_active=True,
            settings=org_settings or {},
            workspace_id=resolved_workspace_id,
        )
        db.add(org)
        db.commit()
        db.refresh(org)

        user = db.query(User).filter(User.username == "admin").first()

        session = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="PO bridge test session",
            title="PO bridge test session",
            status="completed",
            organization_id=org.id,
            user_id=user.id if user else None,
            project_app=project_app,
            project_id=project_id,
            processing_metadata=processing_metadata,
            started_at=datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc),
            transcript_simple="hello project ops",
            duration=120.0,
            participants=[],
            final_summary={"executive": "x", "bullets": [], "actions": []},
        )
        db.add(session)
        db.commit()
        db.refresh(session)

        if items is None:
            items = [
                {"text": "Finalize Q3 budget by Friday", "owner": "Alice"},
                {"text": "Document onboarding flow", "owner": "Bob"},
                {"text": "Schedule follow-up sync", "owner": None},
            ]
        for idx, spec in enumerate(items):
            db.add(
                ActionItem(
                    session_id=session.id,
                    organization_id=org.id,
                    text=spec["text"],
                    owner=spec.get("owner"),
                    status=spec.get("status", "todo"),
                    due_date=spec.get("due_date"),
                    sort_order=idx,
                    source="final_summary",
                    raw_payload=spec.get("raw_payload"),
                )
            )
        db.commit()
        return org.id, session.id
    finally:
        db.close()


def _items_for_session(session_id: int):
    from database.database import SessionLocal
    from database.models import ActionItem

    db = SessionLocal()
    try:
        return (
            db.query(ActionItem)
            .filter(ActionItem.session_id == session_id)
            .order_by(ActionItem.sort_order)
            .all()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. Happy path — 3 creates + stamps
# ---------------------------------------------------------------------------


def test_creates_tasks_and_stamps(client):
    from services.projectops_writer import write_action_items_to_projectops

    org_id, session_id = _seed(
        org_settings={"projectops_default_project_number": "P-00055"},
        items=[
            {
                "text": "Finalize Q3 budget",
                "owner": "Alice",
                "due_date": datetime(2026, 6, 1, tzinfo=timezone.utc),
            },
            {"text": "Document onboarding flow", "owner": "Bob"},
            {"text": "Schedule follow-up sync", "owner": None},
        ],
    )
    stub = _FakeProjectOpsClient(task_id="task-fixed-uuid", project_number="P-00055")

    from database.database import SessionLocal

    db = SessionLocal()
    try:
        result = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "live"
    assert result.created == 3
    assert result.skipped == 0

    # 3 create_task calls, each with the resolved target + the item text.
    assert len(stub.created) == 3
    assert {c["project_number"] for c in stub.created} == {"P-00055"}
    assert stub.created[0]["title"] == "Finalize Q3 budget"
    assert stub.created[0]["priority"] == "MEDIUM"
    assert stub.created[0]["type"] == "OTHER"
    # Description carries provenance.
    assert "From meeting:" in stub.created[0]["description"]
    assert "owner: Alice" in stub.created[0]["description"]
    # due_date threaded through (raw datetime; the real client normalizes to
    # ISO). SQLite drops tzinfo on DateTime(timezone=True) round-trip, so
    # compare the calendar date rather than an exact tz-aware instant.
    due = stub.created[0]["due_date"]
    assert due is not None and (due.year, due.month, due.day) == (2026, 6, 1)

    # Every row stamped.
    items = _items_for_session(session_id)
    assert len(items) == 3
    for it in items:
        assert it.raw_payload is not None
        assert it.raw_payload["po_task_id"] == "task-fixed-uuid"
        assert it.raw_payload["po_project_number"] == "P-00055"
        assert it.raw_payload["po_synced_at"].endswith("Z")


# ---------------------------------------------------------------------------
# 2. Replay — re-run skips all, creates none
# ---------------------------------------------------------------------------


def test_replay_skips_already_synced(client):
    from services.projectops_writer import write_action_items_to_projectops
    from database.database import SessionLocal

    org_id, session_id = _seed(
        org_settings={"projectops_default_project_number": "P-00055"}
    )

    # First run stamps all 3.
    first = _FakeProjectOpsClient()
    db = SessionLocal()
    try:
        r1 = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=first
            )
        )
    finally:
        db.close()
    assert r1.created == 3 and r1.skipped == 0
    assert len(first.created) == 3

    # Second run: a fresh stub sees the stamps and creates nothing.
    second = _FakeProjectOpsClient()
    db = SessionLocal()
    try:
        r2 = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=second
            )
        )
    finally:
        db.close()

    assert r2.ok is True
    assert r2.mode == "live"
    assert r2.created == 0
    assert r2.skipped == 3
    assert second.created == []  # no duplicate tasks
    # Items remain Meeting-Ops-local; no task-status write is attempted.
    assert second.status_updates == []


# ---------------------------------------------------------------------------
# 3. No-op when PROJECTOPS_API_KEY unset
# ---------------------------------------------------------------------------


def test_noop_when_unconfigured(client):
    from services.projectops_client import ProjectOpsClient
    from services.projectops_writer import write_action_items_to_projectops
    from database.database import SessionLocal

    org_id, session_id = _seed(
        org_settings={"projectops_default_project_number": "P-00055"}
    )

    # A real client with no env key is log-only.
    probe = ProjectOpsClient()
    assert probe.is_live is False

    db = SessionLocal()
    try:
        result = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=probe
            )
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "no-op"
    assert result.created == 0
    assert result.skipped == 0

    # Nothing got stamped (no client calls happened).
    for it in _items_for_session(session_id):
        assert (it.raw_payload or {}).get("po_task_id") is None


# ---------------------------------------------------------------------------
# 4. Failure path — ok=False, rows unchanged, no exception
# ---------------------------------------------------------------------------


def test_failure_returns_ok_false_rows_unchanged(client):
    from services.projectops_writer import write_action_items_to_projectops
    from database.database import SessionLocal

    org_id, session_id = _seed(
        org_settings={"projectops_default_project_number": "P-00055"}
    )
    failing = _FakeProjectOpsClient(fail=True)

    db = SessionLocal()
    try:
        # Must NOT raise.
        result = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=failing
            )
        )
    finally:
        db.close()

    assert result.ok is False
    assert result.mode == "error"
    assert result.created == 0

    # Rows left untouched — nothing stamped.
    for it in _items_for_session(session_id):
        assert (it.raw_payload or {}).get("po_task_id") is None


# ---------------------------------------------------------------------------
# 5. No target resolved — mode="no-target"
# ---------------------------------------------------------------------------


def test_no_target_when_unresolved(client):
    from services.projectops_writer import write_action_items_to_projectops
    from database.database import SessionLocal

    # No org default, no override, no session->PO link.
    org_id, session_id = _seed()
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        result = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "no-target"
    assert result.created == 0
    assert result.skipped == 0
    assert stub.created == []  # writer returns before any client call


# ---------------------------------------------------------------------------
# 6. Session->PO link resolves the target (UUID used directly)
# ---------------------------------------------------------------------------


def test_session_link_resolves_target(client):
    from services.projectops_writer import write_action_items_to_projectops
    from database.database import SessionLocal

    project_uuid = "abcdef12-3456-4789-8abc-def012345678"
    # No org default — resolution must come from the session link.
    org_id, session_id = _seed(
        project_app="project-ops",
        project_id=project_uuid,
    )
    stub = _FakeProjectOpsClient(project_number="P-00099")

    db = SessionLocal()
    try:
        result = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.created == 3
    # The session's PO project UUID was fed to create_task verbatim.
    assert {c["project_number"] for c in stub.created} == {project_uuid}
    # Stamp uses the project number echoed back by the create response.
    for it in _items_for_session(session_id):
        assert it.raw_payload["po_project_number"] == "P-00099"


# ---------------------------------------------------------------------------
# 7. Per-meeting override beats the org default
# ---------------------------------------------------------------------------


def test_override_beats_org_default(client):
    from services.projectops_writer import write_action_items_to_projectops
    from database.database import SessionLocal

    org_id, session_id = _seed(
        org_settings={"projectops_default_project_number": "P-00001"},
        processing_metadata={"po_project_override": "P-00077"},
    )
    stub = _FakeProjectOpsClient(project_number="P-00077")

    db = SessionLocal()
    try:
        result = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.created == 3
    assert {c["project_number"] for c in stub.created} == {"P-00077"}


# ---------------------------------------------------------------------------
# 8. Local completion never propagates Project-Ops task completion
# ---------------------------------------------------------------------------


def test_done_status_does_not_propagate_on_replay(client):
    from services.projectops_writer import write_action_items_to_projectops
    from database.database import SessionLocal

    # Items already bridged (po_task_id stamped) AND locally marked done.
    org_id, session_id = _seed(
        org_settings={"projectops_default_project_number": "P-00055"},
        items=[
            {
                "text": "Already done item",
                "owner": "Alice",
                "status": "done",
                "raw_payload": {"po_task_id": "task-A", "po_project_number": "P-00055"},
            },
            {
                "text": "Still open item",
                "owner": "Bob",
                "status": "todo",
                "raw_payload": {"po_task_id": "task-B", "po_project_number": "P-00055"},
            },
        ],
    )
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        result = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.created == 0
    assert result.skipped == 2
    # Project-Ops owns task status. Even a linked local "done" item produces no
    # task-status request.
    assert stub.status_updates == []


# ---------------------------------------------------------------------------
# 9. Auto-push opt-in OFF (default) — mode="gated-off", zero client calls
# ---------------------------------------------------------------------------


def test_gated_off_when_auto_push_disabled(client, monkeypatch):
    """Default posture: the automatic action-item push is OPT-IN. With the
    auto-push gate OFF the writer returns mode="gated-off" + created=0 and
    makes ZERO create_task calls — even when a target project resolves and
    a live client is injected. This is the inverse of every other live-path
    test (which run with the gate forced ON by the autouse fixture); here we
    re-patch the same resolver seam back to disabled so the opt-in gate has
    explicit coverage in both states.
    """
    from services.projectops_writer import write_action_items_to_projectops
    from database.database import SessionLocal

    # Re-disable the gate (the autouse _enable_auto_push fixture turned it
    # on); this monkeypatch override wins and is restored on teardown.
    _patch_auto_push(monkeypatch, enabled=False)

    # A resolvable target + a live stub: proves the gate — not a missing
    # project or an unconfigured client — is what stops the push.
    org_id, session_id = _seed(
        org_settings={"projectops_default_project_number": "P-00055"}
    )
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        result = _run(
            write_action_items_to_projectops(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "gated-off"
    assert result.created == 0
    assert result.skipped == 0
    # The push never fired: no task creates, no status nudges.
    assert stub.created == []
    assert stub.status_updates == []

    # And nothing got stamped onto the action items.
    for it in _items_for_session(session_id):
        assert (it.raw_payload or {}).get("po_task_id") is None


# ---------------------------------------------------------------------------
# 10-17. Triage submit path (submit_action_items_to_triage)
#
# The finalize/reprocess pipeline now submits action items to the PO TRIAGE
# inbox (propose-only) instead of creating tasks directly. These cover the
# new writer's contract: submit + stamp, replay-skip, no-target-still-works
# (the key win over the direct writer), the linked-project routing hint, the
# shared auto-push gate, eventual-consistency on failure (don't stamp), and
# not double-filing direct-bridged items.
# ---------------------------------------------------------------------------


def test_triage_submits_and_stamps(client):
    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    # No target project anywhere — triage needs none.
    org_id, session_id = _seed(
        items=[
            {"text": "Investigate slow dashboard", "owner": "Shafen"},
            {"text": "Write API docs for triage", "owner": "Alice"},
            {"text": "Email the client the timeline", "owner": None},
        ],
    )
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        result = _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "live"
    assert result.created == 3  # po_created echoed from the stub

    # One batch submit carrying all 3 candidates.
    assert len(stub.submitted) == 1
    batch = stub.submitted[0]
    assert batch["source_type"] == "MEETING_OPS"
    cands = batch["items"]
    assert len(cands) == 3
    assert cands[0]["text"] == "Investigate slow dashboard"
    assert cands[0]["owner"] == "Shafen"
    assert cands[0]["sourceActionItemId"]  # a stable id is present
    assert cands[0]["sourceRef"]["app"] == "meeting-ops"
    assert "meetingTitle" in cands[0]["sourceRef"]
    # No create_task calls happen on the triage path.
    assert stub.created == []

    # Every row stamped for triage — but NOT with a po_task_id (no task yet).
    for it in _items_for_session(session_id):
        rp = it.raw_payload or {}
        assert rp.get("po_triage_submitted_at", "").endswith("Z")
        assert rp.get("po_triage_source_type") == "MEETING_OPS"
        assert rp.get("po_task_id") is None
        assert it.project_ops_link_state == "proposed"
        assert it.project_ops_last_synced_at is not None


def test_triage_replay_skips_submitted(client):
    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    org_id, session_id = _seed()

    first = _FakeProjectOpsClient()
    db = SessionLocal()
    try:
        r1 = _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=first
            )
        )
    finally:
        db.close()
    assert r1.created == 3
    assert len(first.submitted) == 1

    # Second run: stamps are seen; nothing re-submitted.
    second = _FakeProjectOpsClient()
    db = SessionLocal()
    try:
        r2 = _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=second
            )
        )
    finally:
        db.close()
    assert r2.ok is True
    assert r2.mode == "live"
    assert r2.created == 0
    assert r2.skipped == 3
    assert second.submitted == []


def test_triage_no_target_still_submits(client):
    # The key improvement over the direct writer: no session link, no
    # override, and no org default — triage STILL submits (it routes itself,
    # where the direct writer would return mode="no-target").
    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    org_id, session_id = _seed()  # bare org, zero project config
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        result = _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "live"
    assert result.created == 3
    assert len(stub.submitted) == 1


def test_triage_passes_linked_project_hint(client):
    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    project_uuid = "abcdef12-3456-4789-8abc-def012345678"
    org_id, session_id = _seed(
        project_app="project-ops", project_id=project_uuid
    )
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    cands = stub.submitted[0]["items"]
    assert all(
        c["sourceRef"]["linkedProjectRef"] == project_uuid for c in cands
    )


def test_triage_gated_off_when_auto_push_disabled(client, monkeypatch):
    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    _patch_auto_push(monkeypatch, enabled=False)

    org_id, session_id = _seed()
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        result = _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "gated-off"
    assert result.created == 0
    assert stub.submitted == []
    for it in _items_for_session(session_id):
        assert (it.raw_payload or {}).get("po_triage_submitted_at") is None


def test_triage_failure_leaves_rows_unstamped(client):
    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    org_id, session_id = _seed()
    failing = _FakeProjectOpsClient(fail=True)

    db = SessionLocal()
    try:
        result = _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=failing
            )
        )
    finally:
        db.close()

    assert result.ok is False
    assert result.mode == "error"
    # No submission stamp, but the recoverable failure is visible per item.
    for it in _items_for_session(session_id):
        assert (it.raw_payload or {}).get("po_triage_submitted_at") is None
        assert it.project_ops_link_state == "sync_failed"
        assert it.project_ops_sync_error == "PROJECT_OPS_UNAVAILABLE"


def test_triage_skips_direct_bridged_items(client):
    # An item already bridged via the direct path (po_task_id set) is left
    # alone — never double-filed into the triage inbox.
    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    org_id, session_id = _seed(
        items=[
            {
                "text": "Already a PO task",
                "owner": "Alice",
                "raw_payload": {"po_task_id": "task-X"},
            },
            {"text": "Fresh item", "owner": "Bob"},
        ],
    )
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        result = _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.ok is True
    # Only the fresh item submitted; the direct-bridged one skipped.
    assert len(stub.submitted) == 1
    cands = stub.submitted[0]["items"]
    assert len(cands) == 1
    assert cands[0]["text"] == "Fresh item"
    assert result.skipped == 1  # the already-bridged item


# ---------------------------------------------------------------------------
# 18-20. Triage federation token (workspace-bound, aud=project-ops)
#
# The triage push must carry a Brigade-vouched, workspace-bound token so PO
# routes the proposals to the correct tenant. These cover: (a) when the org
# has a workspace_id and the federation token mints, it's threaded to the
# client as bearer_override; (b) when the org has NO workspace_id, nothing is
# sent; (c) when token exchange yields nothing, nothing is sent. A delayed,
# retryable proposal is safer than routing work through a default tenant.
#
# The writer imports ``projectops_federation_token`` lazily from
# ``services.projectops_token`` inside the function body, so we patch it at
# the source module (patching the name on ``services.projectops_writer``
# would not be seen) — same seam pattern as ``_patch_auto_push``.
# ---------------------------------------------------------------------------


def _patch_federation_token(monkeypatch, *, returns):
    """Replace ``projectops_federation_token`` (recording every workspace_id
    it was asked for). ``returns`` is the token string to hand back, or None."""
    from services import projectops_token as fed

    asked: list[str] = []

    async def _fake(workspace_id: str):
        asked.append(workspace_id)
        return returns

    monkeypatch.setattr(fed, "projectops_federation_token", _fake)
    return asked


def test_triage_uses_federation_bearer_when_workspace_resolves(client, monkeypatch):
    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    asked = _patch_federation_token(monkeypatch, returns="fed-bearer-tok")

    org_id, session_id = _seed(workspace_id="ws-tenant-1")
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        result = _run(
            submit_action_items_to_triage(
                db=db, session_pk=session_id, client=stub
            )
        )
    finally:
        db.close()

    assert result.ok is True
    assert result.mode == "live"
    # The federation token was minted for the org's workspace_id...
    assert asked == ["ws-tenant-1"]
    # ...and threaded to the client as the per-call bearer override.
    assert len(stub.submitted) == 1
    assert stub.submitted[0]["bearer_override"] == "fed-bearer-tok"
    # Items still get stamped (happy path).
    for it in _items_for_session(session_id):
        assert (it.raw_payload or {}).get("po_triage_submitted_at", "").endswith("Z")


def test_triage_exchange_bearer_drives_unconfigured_real_client(
    client, monkeypatch, _enable_auto_push
):
    """The Brigade exchange is the only credential required by triage."""
    from database.database import SessionLocal
    from services import projectops_token
    from services.projectops_client import ProjectOpsClient
    from services.projectops_writer import submit_action_items_to_triage

    monkeypatch.setenv("MEETING_OPS_KC_CLIENT_ID", "meeting-ops")
    monkeypatch.setenv("MEETING_OPS_KC_CLIENT_SECRET", "meeting-secret")
    monkeypatch.setenv("MEETING_OPS_KC_TOKEN_URL", "https://auth.test/token")
    monkeypatch.setenv(
        "BRIGADE_EXCHANGE_URL",
        "https://brigade.test/api/v1/federation/token",
    )
    monkeypatch.setattr(
        projectops_token,
        "projectops_federation_token",
        _enable_auto_push,
    )
    projectops_token._token_cache.clear()
    real_async_client = httpx.AsyncClient

    class _TokenResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _TokenClient:
        def __init__(self):
            self.posts: list[dict] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **kwargs):
            self.posts.append({"url": url, "data": kwargs.get("data")})
            if len(self.posts) == 1:
                return _TokenResponse(
                    {"access_token": "meeting-subject", "expires_in": 300}
                )
            return _TokenResponse(
                {"access_token": "workspace-bearer", "expires_in": 300}
            )

    token_http = _TokenClient()
    monkeypatch.setattr(
        projectops_token.httpx,
        "AsyncClient",
        lambda *_args, **_kwargs: token_http,
    )

    ingest_requests: list[httpx.Request] = []

    def ingest_handler(request: httpx.Request) -> httpx.Response:
        ingest_requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "created": len(body["items"]),
                "skipped": 0,
                "proposals": [
                    {
                        "id": f"proposal-{item['sourceActionItemId']}",
                        "sourceActionItemId": item["sourceActionItemId"],
                    }
                    for item in body["items"]
                ],
            },
        )

    org_id, session_id = _seed(workspace_id="ws-exchange-only")

    async def exercise():
        async with real_async_client(
            transport=httpx.MockTransport(ingest_handler)
        ) as ingest_http:
            unconfigured = ProjectOpsClient(
                base_url="https://projectops.example.test/api/v1",
                _client=ingest_http,
            )
            assert unconfigured.is_live is False
            db = SessionLocal()
            try:
                return await submit_action_items_to_triage(
                    db=db,
                    session_pk=session_id,
                    client=unconfigured,
                )
            finally:
                db.close()

    try:
        result = _run(exercise())
    finally:
        projectops_token._token_cache.clear()

    assert result.ok is True
    assert result.mode == "live"
    assert len(token_http.posts) == 2
    assert token_http.posts[1]["data"]["workspace_id"] == "ws-exchange-only"
    assert len(ingest_requests) == 1
    assert ingest_requests[0].headers["authorization"] == "Bearer workspace-bearer"


def test_triage_fails_closed_when_no_workspace_id(client, monkeypatch, caplog):
    import logging

    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    # Federation token would mint if asked — but it must NOT be asked when the
    # org has no workspace_id.
    asked = _patch_federation_token(monkeypatch, returns="should-not-be-used")

    org_id, session_id = _seed(workspace_id=None)  # no tenant on the org
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        with caplog.at_level(logging.WARNING):
            result = _run(
                submit_action_items_to_triage(
                    db=db, session_pk=session_id, client=stub
                )
            )
    finally:
        db.close()

    assert result.ok is False
    assert result.mode == "error"
    assert "workspace is not provisioned" in (result.detail or "")
    # No workspace_id -> never mint a federation token.
    assert asked == []
    assert stub.submitted == []
    assert all(
        (item.raw_payload or {}).get("po_triage_submitted_at") is None
        for item in _items_for_session(session_id)
    )
    assert all(
        item.project_ops_link_state == "sync_failed"
        and item.project_ops_sync_error == "WORKSPACE_NOT_PROVISIONED"
        for item in _items_for_session(session_id)
    )
    assert any("workspace is not provisioned" in rec.getMessage() for rec in caplog.records)


def test_triage_fails_closed_when_federation_token_unavailable(
    client, monkeypatch, caplog
):
    import logging

    from services.projectops_writer import submit_action_items_to_triage
    from database.database import SessionLocal

    # Org HAS a workspace_id, but the exchange yields nothing (Brigade not yet
    # granting actor=meeting-ops -> aud=project-ops, or KC secret unset).
    asked = _patch_federation_token(monkeypatch, returns=None)

    org_id, session_id = _seed(workspace_id="ws-tenant-2")
    stub = _FakeProjectOpsClient()

    db = SessionLocal()
    try:
        with caplog.at_level(logging.WARNING):
            result = _run(
                submit_action_items_to_triage(
                    db=db, session_pk=session_id, client=stub
                )
            )
    finally:
        db.close()

    assert result.ok is False
    assert result.mode == "error"
    assert "federation token is unavailable" in (result.detail or "")
    assert asked == ["ws-tenant-2"]
    assert stub.submitted == []
    assert all(
        (item.raw_payload or {}).get("po_triage_submitted_at") is None
        for item in _items_for_session(session_id)
    )
    assert all(
        item.project_ops_link_state == "sync_failed"
        and item.project_ops_sync_error == "FEDERATION_TOKEN_UNAVAILABLE"
        for item in _items_for_session(session_id)
    )
    assert any(
        "workspace-bound federation token is unavailable" in rec.getMessage()
        for rec in caplog.records
    )
