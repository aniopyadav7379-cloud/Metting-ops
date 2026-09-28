from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest


@pytest.mark.asyncio
async def test_batch_and_interactive_jobs_use_distinct_queues():
    from services import job_runner
    from workers import bulk_import_worker

    pool = AsyncMock()
    pool.enqueue_job = AsyncMock(side_effect=[
        MagicMock(job_id="interactive-job"),
        MagicMock(job_id="batch-job"),
    ])
    with patch.object(job_runner, "get_arq_pool", AsyncMock(return_value=pool)), \
         patch.object(bulk_import_worker, "get_arq_pool", AsyncMock(return_value=pool)):
        await job_runner.enqueue_job("finalize_session_job", 1)
        await bulk_import_worker.enqueue_file(uuid.uuid4())

    interactive_call, batch_call = pool.enqueue_job.await_args_list
    assert interactive_call.kwargs["_queue_name"] == job_runner.INTERACTIVE_QUEUE_NAME
    assert batch_call.kwargs["_queue_name"] == bulk_import_worker.BATCH_QUEUE_NAME
    assert interactive_call.kwargs["_queue_name"] != batch_call.kwargs["_queue_name"]


def test_interactive_worker_has_reserved_capacity():
    from workers.bulk_import_worker import InteractiveWorkerSettings, WorkerSettings

    assert InteractiveWorkerSettings.queue_name != WorkerSettings.queue_name
    assert InteractiveWorkerSettings.max_jobs >= 1
