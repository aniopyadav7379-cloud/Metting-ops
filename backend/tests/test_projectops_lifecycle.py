from __future__ import annotations

import asyncio
import uuid

import pytest


@pytest.fixture(scope="module", autouse=True)
def _lifecycle_schema():
    """Keep reconciliation tests hermetic without booting NPU-only routers."""
    from auth import models as auth_models  # noqa: F401
    from database import models_rooms  # noqa: F401
    from database.database import engine
    from database.models import Base

    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


class _LifecycleClient:
    def __init__(self) -> None:
        self.lifecycle_calls: list[dict] = []
        self.submit_calls: list[dict] = []
        self.closed = False

    async def get_source_lifecycle(
        self,
        source_action_item_ids,
        *,
        bearer_override,
    ):
        self.lifecycle_calls.append(
            {
                "ids": list(source_action_item_ids),
                "bearer": bearer_override,
            }
        )
        return {
            "version": "meeting-ops.action-lifecycle.v1",
            "correlationId": "corr-test",
            "items": [
                {
                    "sourceActionItemId": source_id,
                    "proposalId": f"proposal-{source_id}",
                    "state": "approved_linked",
                    "proposalStatus": "APPLIED",
                    "taskId": f"task-{source_id}",
                    "taskUrl": (
                        "https://projectops.example.test/dashboard/tasks/"
                        f"task-{source_id}"
                    ),
                    "taskStatus": "IN_PROGRESS",
                    "projectNumber": "P-00041",
                    "errorCode": None,
                    "updatedAt": "2026-07-24T15:00:00.000Z",
                }
                for source_id in source_action_item_ids
            ],
        }

    async def submit_candidate_action_items(
        self,
        items,
        *,
        source_type,
        bearer_override,
    ):
        self.submit_calls.append(
            {
                "items": list(items),
                "source_type": source_type,
                "bearer": bearer_override,
            }
        )
        return {
            "created": 1,
            "skipped": 0,
            "proposals": [
                {
                    "id": f"proposal-{items[0]['sourceActionItemId']}",
                    "sourceActionItemId": items[0]["sourceActionItemId"],
                }
            ],
        }

    async def aclose(self):
        self.closed = True


def _run(coro):
    return asyncio.run(coro)


def _seed_world():
    from auth.models import Organization
    from database.database import SessionLocal
    from database.models import ActionItem, RecordingSession

    db = SessionLocal()
    suffix = uuid.uuid4().hex[:8]
    try:
        org_a = Organization(
            name=f"Lifecycle A {suffix}",
            slug=f"lifecycle-a-{suffix}",
            is_active=True,
            workspace_id=f"workspace-a-{suffix}",
        )
        org_b = Organization(
            name=f"Lifecycle B {suffix}",
            slug=f"lifecycle-b-{suffix}",
            is_active=True,
            workspace_id=f"workspace-b-{suffix}",
        )
        db.add_all([org_a, org_b])
        db.commit()
        db.refresh(org_a)
        db.refresh(org_b)

        session_a = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="Lifecycle A",
            organization_id=org_a.id,
            status="completed",
            duration=60,
        )
        session_b = RecordingSession(
            session_id=str(uuid.uuid4()),
            name="Lifecycle B",
            organization_id=org_b.id,
            status="completed",
            duration=60,
        )
        db.add_all([session_a, session_b])
        db.commit()
        db.refresh(session_a)
        db.refresh(session_b)

        item_a = ActionItem(
            session_id=session_a.id,
            organization_id=org_a.id,
            text="Prepare lifecycle evidence",
            status="done",
            source="manual",
            project_ops_link_state="proposed",
        )
        item_b = ActionItem(
            session_id=session_b.id,
            organization_id=org_b.id,
            text="Other workspace item",
            status="todo",
            source="manual",
            project_ops_link_state="proposed",
        )
        db.add_all([item_a, item_b])
        db.commit()
        db.refresh(item_a)
        db.refresh(item_b)
        return {
            "org_a": org_a.id,
            "org_b": org_b.id,
            "session_a": session_a.id,
            "session_b": session_b.id,
            "item_a": item_a.id,
            "item_b": item_b.id,
        }
    finally:
        db.close()


def _patch_token(monkeypatch):
    from services import projectops_token

    asked: list[str] = []
    monkeypatch.setenv("PROJECTOPS_PUBLIC_URL", "https://projectops.example.test")

    async def token(workspace_id: str):
        asked.append(workspace_id)
        return f"federation-for-{workspace_id}"

    monkeypatch.setattr(projectops_token, "projectops_federation_token", token)
    return asked


def test_reconcile_links_task_without_mutating_local_completion(monkeypatch):
    from database.database import SessionLocal
    from database.models import ActionItem
    from services.projectops_lifecycle import reconcile_projectops_action_items

    world = _seed_world()
    asked = _patch_token(monkeypatch)
    upstream = _LifecycleClient()

    db = SessionLocal()
    try:
        result = _run(
            reconcile_projectops_action_items(
                db=db,
                organization_id=world["org_a"],
                item_ids=[world["item_a"]],
                limit=1,
                client=upstream,
            )
        )
        item = db.query(ActionItem).filter(ActionItem.id == world["item_a"]).one()
        assert result == {"requested": 1, "reconciled": 1, "failed": 0}
        assert item.status == "done"  # Meeting-Ops ownership is untouched.
        assert item.project_ops_link_state == "approved_linked"
        assert item.project_ops_task_id == f"task-{item.id}"
        assert item.project_ops_task_url.endswith(f"/task-{item.id}")
        assert item.project_ops_task_status == "IN_PROGRESS"
        assert item.project_ops_last_synced_at is not None
    finally:
        db.close()

    assert len(asked) == 1
    assert upstream.lifecycle_calls[0]["ids"] == [str(world["item_a"])]


def test_reconcile_cannot_read_or_update_another_organization(monkeypatch):
    from database.database import SessionLocal
    from database.models import ActionItem
    from services.projectops_lifecycle import reconcile_projectops_action_items

    world = _seed_world()
    _patch_token(monkeypatch)
    upstream = _LifecycleClient()

    db = SessionLocal()
    try:
        result = _run(
            reconcile_projectops_action_items(
                db=db,
                organization_id=world["org_a"],
                item_ids=[world["item_b"]],
                limit=1,
                client=upstream,
            )
        )
        other = db.query(ActionItem).filter(ActionItem.id == world["item_b"]).one()
        assert result == {"requested": 0, "reconciled": 0, "failed": 0}
        assert other.project_ops_link_state == "proposed"
        assert other.project_ops_task_id is None
    finally:
        db.close()

    assert upstream.lifecycle_calls == []


def test_session_refresh_is_bounded_and_cannot_cross_organizations(monkeypatch):
    from database.database import SessionLocal
    from services.projectops_lifecycle import (
        reconcile_projectops_session_action_items,
    )

    world = _seed_world()
    _patch_token(monkeypatch)
    upstream = _LifecycleClient()
    db = SessionLocal()
    try:
        result = _run(
            reconcile_projectops_session_action_items(
                db=db,
                organization_id=world["org_a"],
                session_id=world["session_a"],
                client=upstream,
            )
        )
        assert result == {
            "requested": 1,
            "reconciled": 1,
            "failed": 0,
            "item_ids": [world["item_a"]],
            "truncated": False,
        }
        assert upstream.lifecycle_calls[0]["ids"] == [str(world["item_a"])]

        with pytest.raises(LookupError):
            _run(
                reconcile_projectops_session_action_items(
                    db=db,
                    organization_id=world["org_a"],
                    session_id=world["session_b"],
                    client=upstream,
                )
            )
        with pytest.raises(ValueError):
            _run(
                reconcile_projectops_session_action_items(
                    db=db,
                    organization_id=world["org_a"],
                    session_id=world["session_a"],
                    limit=101,
                    client=upstream,
                )
            )
    finally:
        db.close()

    assert len(upstream.lifecycle_calls) == 1


def test_requeue_is_one_item_and_replay_does_not_resubmit(monkeypatch):
    from database.database import SessionLocal
    from database.models import ActionItem
    from services.projectops_lifecycle import requeue_projectops_action_item

    world = _seed_world()
    _patch_token(monkeypatch)
    upstream = _LifecycleClient()

    db = SessionLocal()
    try:
        item = db.query(ActionItem).filter(ActionItem.id == world["item_a"]).one()
        item.project_ops_link_state = "sync_failed"
        item.project_ops_sync_error = "PROJECT_OPS_UNAVAILABLE"
        db.commit()

        _run(
            requeue_projectops_action_item(
                db=db,
                organization_id=world["org_a"],
                item_id=world["item_a"],
                client=upstream,
            )
        )
        _run(
            requeue_projectops_action_item(
                db=db,
                organization_id=world["org_a"],
                item_id=world["item_a"],
                client=upstream,
            )
        )
        db.refresh(item)
        assert item.project_ops_link_state == "approved_linked"
        assert item.status == "done"
    finally:
        db.close()

    assert len(upstream.submit_calls) == 1
    assert len(upstream.submit_calls[0]["items"]) == 1
    assert upstream.submit_calls[0]["items"][0]["sourceActionItemId"] == str(
        world["item_a"]
    )
    assert len(upstream.lifecycle_calls) == 2
