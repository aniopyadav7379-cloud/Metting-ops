"""Async job status endpoint — v3.18.3.

Generic poll target for the four endpoints (always-on finalize, digest
generation, TTS summary render, TTS podcast render) that switched to
"enqueue + 202 + frontend polls" in v3.18.3 to dodge Cloudflare's 120s
edge timeout. The frontend's `pollJob()` helper hits this endpoint on
an exponential backoff (2s -> 30s) until status flips out of
pending/running.

Auth: any authenticated user can read any job_id they hold. We don't
gate by org because arq job IDs are random UUIDs that aren't enumerable
and the worker functions themselves don't include sensitive payload
content in the result (just status + IDs + summary counts). If a tighter
ownership check is needed later we can wrap arq job metadata with our
own owner_user_id table.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from services.job_runner import get_job_owner, get_job_status

router = APIRouter(prefix="/api/jobs", tags=["Jobs"])
logger = logging.getLogger(__name__)


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> dict:
    """Status snapshot for an async job.

    Returns:
        {
          "job_id": str,
          "status": "pending"|"running"|"completed"|"failed",
          "result": <worker-return-value> or null,
          "error": str or null,
          "queued_at": isoformat or null,
          "started_at": isoformat or null,
          "finished_at": isoformat or null,
        }

    404 when the job ID is unknown to arq (already evicted from the
    keep_result window, or never existed).
    """
    owner = await get_job_owner(job_id)
    if not current_user.is_superuser:
        if owner is None:
            raise HTTPException(status_code=404, detail="Job not found.")
        if owner["org_id"] != active_org.organization.id:
            raise HTTPException(status_code=404, detail="Job not found.")
        if owner["user_id"] != current_user.id:
            raise HTTPException(status_code=403, detail="Job belongs to another user.")

    payload = await get_job_status(job_id)
    if payload.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Job not found.")
    return payload
