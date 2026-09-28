from __future__ import annotations

from types import SimpleNamespace


def _item(**overrides):
    values = {
        "project_ops_link_state": "proposed",
        "project_ops_proposal_id": "proposal-41",
        "project_ops_task_id": None,
        "project_ops_task_url": None,
        "project_ops_project_number": None,
        "project_ops_task_status": None,
        "project_ops_submitted_at": None,
        "project_ops_last_sync_attempt_at": None,
        "project_ops_last_synced_at": None,
        "project_ops_remote_updated_at": None,
        "project_ops_sync_error": None,
        "project_ops_retry_count": 0,
        "raw_payload": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _approved_record(**overrides):
    values = {
        "state": "approved_linked",
        "proposalId": "proposal-41",
        "taskId": "task-41",
        "taskUrl": "https://projectops.unicorncommander.ai/dashboard/tasks/task-41",
        "taskStatus": "IN_PROGRESS",
        "projectNumber": "P-00041",
        "updatedAt": "2026-07-24T15:00:00.000Z",
    }
    values.update(overrides)
    return values


def test_lifecycle_rejects_cross_origin_task_link(monkeypatch):
    from services.projectops_lifecycle import _apply_lifecycle_record

    monkeypatch.setenv("PROJECTOPS_PUBLIC_URL", "https://projectops.unicorncommander.ai")
    item = _item()
    _apply_lifecycle_record(
        item,
        _approved_record(taskUrl="https://phish.example/dashboard/tasks/task-41"),
    )

    assert item.project_ops_link_state == "sync_failed"
    assert item.project_ops_sync_error == "INVALID_TASK_LINK"
    assert item.project_ops_task_url is None


def test_lifecycle_rejects_unknown_task_status(monkeypatch):
    from services.projectops_lifecycle import _apply_lifecycle_record

    monkeypatch.setenv("PROJECTOPS_PUBLIC_URL", "https://projectops.unicorncommander.ai")
    item = _item()
    _apply_lifecycle_record(item, _approved_record(taskStatus="REOPENED"))

    assert item.project_ops_link_state == "sync_failed"
    assert item.project_ops_sync_error == "INVALID_TASK_STATUS"


def test_lifecycle_rejects_task_url_bound_to_a_different_task(monkeypatch):
    from services.projectops_lifecycle import _apply_lifecycle_record

    monkeypatch.setenv("PROJECTOPS_PUBLIC_URL", "https://projectops.unicorncommander.ai")
    item = _item()
    _apply_lifecycle_record(
        item,
        _approved_record(
            taskUrl="https://projectops.unicorncommander.ai/dashboard/tasks/task-99"
        ),
    )

    assert item.project_ops_link_state == "sync_failed"
    assert item.project_ops_sync_error == "INVALID_TASK_LINK"
    assert item.project_ops_task_url is None


def test_lifecycle_rejects_encoded_route_separators(monkeypatch):
    from services.projectops_lifecycle import _apply_lifecycle_record

    monkeypatch.setenv("PROJECTOPS_PUBLIC_URL", "https://projectops.unicorncommander.ai")
    for suffix in ("task-41%2Fother", "task-41%5Cother", "task-41%252Fother"):
        item = _item()
        _apply_lifecycle_record(
            item,
            _approved_record(
                taskUrl=(
                    "https://projectops.unicorncommander.ai/dashboard/tasks/"
                    + suffix
                )
            ),
        )
        assert item.project_ops_link_state == "sync_failed"
        assert item.project_ops_sync_error == "INVALID_TASK_LINK"
        assert item.project_ops_task_url is None


def test_lifecycle_rejects_malformed_remote_timestamp(monkeypatch):
    from services.projectops_lifecycle import _apply_lifecycle_record

    monkeypatch.setenv("PROJECTOPS_PUBLIC_URL", "https://projectops.unicorncommander.ai")
    item = _item()
    _apply_lifecycle_record(item, _approved_record(updatedAt="not-a-timestamp"))

    assert item.project_ops_link_state == "sync_failed"
    assert item.project_ops_sync_error == "INVALID_REMOTE_UPDATED_AT"


def test_terminal_link_cannot_be_reopened_or_have_status_overwritten(monkeypatch):
    from services.projectops_lifecycle import _apply_lifecycle_record

    monkeypatch.setenv("PROJECTOPS_PUBLIC_URL", "https://projectops.unicorncommander.ai")
    item = _item(
        project_ops_link_state="approved_linked",
        project_ops_task_id="task-41",
        project_ops_task_url=(
            "https://projectops.unicorncommander.ai/dashboard/tasks/task-41"
        ),
        project_ops_task_status="COMPLETED",
    )
    _apply_lifecycle_record(
        item,
        {
            "state": "rejected",
            "proposalId": "proposal-41",
            "taskId": None,
            "taskUrl": None,
            "taskStatus": None,
            "projectNumber": "P-00041",
            "updatedAt": "2026-07-24T16:00:00.000Z",
        },
    )

    assert item.project_ops_link_state == "approved_linked"
    assert item.project_ops_task_status == "COMPLETED"
    assert item.project_ops_sync_error == "REMOTE_STATE_REGRESSION"
