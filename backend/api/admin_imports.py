"""Admin pause/resume/cancel endpoints for bulk import (B-import.4).

All endpoints gated by require_role('admin') or is_superuser check.
Separate router mounts under /api/import/admin/ to keep the user-facing
endpoints in imports.py untouched.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import (
    get_current_organization,
    get_current_user,
    require_admin,
)
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import BulkImportFile, BulkImportJob
from services.bulk_import_queue import (
    admin_cancel_job,
    list_all_jobs,
    pause_job,
    resume_job,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/import/admin",
    tags=["Bulk Import Admin"],
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Pydantic response shapes
# ---------------------------------------------------------------------------


class AdminJobFile(BaseModel):
    file_id: str
    original_filename: str
    status: str
    session_id: Optional[str] = None
    error_message: Optional[str] = None
    bytes_total: Optional[int] = None
    created_at: Optional[datetime] = None


class AdminJobResponse(BaseModel):
    job_id: str
    user_id: int
    organization_id: int
    status: str
    total_files: int
    succeeded: int
    failed: int
    skipped: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    created_at: datetime
    files: list[AdminJobFile]


class AdminJobListItem(BaseModel):
    job_id: str
    organization_id: int
    user_id: int
    user_email: Optional[str] = None
    org_name: Optional[str] = None
    status: str
    total_files: int
    succeeded: int
    failed: int
    skipped: int
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    created_at: datetime


class AdminJobListResponse(BaseModel):
    jobs: list[AdminJobListItem]
    total: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/jobs", response_model=AdminJobListResponse)
async def admin_list_jobs(
    status: Optional[str] = Query(None, description="Filter by job status"),
    user_id: Optional[int] = Query(None, description="Filter by user ID"),
    created_after: Optional[datetime] = Query(None, description="Filter by created_at >= value"),
    created_before: Optional[datetime] = Query(None, description="Filter by created_at <= value"),
    limit: int = Query(50, ge=1, le=200, description="Max results"),
    offset: int = Query(0, ge=0, description="Result offset"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AdminJobListResponse:
    """List ALL bulk_import_jobs across all orgs (admin only)."""
    jobs = await list_all_jobs(
        status_filter=status,
        user_id=user_id,
        created_after=created_after,
        created_before=created_before,
        limit=limit,
        offset=offset,
    )

    items = []
    for j in jobs:
        user_email = None
        org_name = None
        if j.user_id:
            user = db.query(User).filter(User.id == j.user_id).first()
            if user:
                user_email = user.email
        from auth.models import Organization
        org = (
            db.query(Organization)
            .filter(Organization.id == j.organization_id)
            .first()
        )
        if org:
            org_name = org.name

        items.append(AdminJobListItem(
            job_id=str(j.id),
            organization_id=j.organization_id,
            user_id=j.user_id,
            user_email=user_email,
            org_name=org_name,
            status=j.status,
            total_files=int(j.total_files or 0),
            succeeded=int(j.succeeded or 0),
            failed=int(j.failed or 0),
            skipped=int(j.skipped or 0),
            started_at=j.started_at,
            finished_at=j.finished_at,
            cancelled_at=j.cancelled_at,
            created_at=j.created_at,
        ))

    return AdminJobListResponse(jobs=items, total=len(items))


@router.post("/jobs/{job_id}/pause")
async def admin_pause_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Pause a processing job. In-flight files finish; queued files stop."""
    ok = await pause_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not running.")
    return {"status": "paused"}


@router.post("/jobs/{job_id}/resume")
async def admin_resume_job(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Resume a paused job. Re-enqueues remaining queued files."""
    ok = await resume_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not paused.")
    return {"status": "processing"}


@router.post("/jobs/{job_id}/cancel")
async def admin_cancel_job_endpoint(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Cancel any job in any org (admin bypass)."""
    ok = await admin_cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"status": "cancelled"}


@router.get("/jobs/{job_id}", response_model=AdminJobResponse)
async def admin_get_job_detail(
    job_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AdminJobResponse:
    """Get full job detail with per-file status (admin cross-org view)."""
    job = (
        db.query(BulkImportJob).filter(BulkImportJob.id == job_id).first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    files = (
        db.query(BulkImportFile)
        .filter(BulkImportFile.job_id == job.id)
        .order_by(BulkImportFile.created_at.asc())
        .all()
    )

    return AdminJobResponse(
        job_id=str(job.id),
        user_id=job.user_id,
        organization_id=job.organization_id,
        status=job.status,
        total_files=int(job.total_files or 0),
        succeeded=int(job.succeeded or 0),
        failed=int(job.failed or 0),
        skipped=int(job.skipped or 0),
        started_at=job.started_at,
        finished_at=job.finished_at,
        cancelled_at=job.cancelled_at,
        created_at=job.created_at,
        files=[
            AdminJobFile(
                file_id=str(f.id),
                original_filename=f.original_filename,
                status=f.status,
                session_id=(
                    str(f.session_id_when_created)
                    if f.session_id_when_created
                    else None
                ),
                error_message=f.error_message,
                bytes_total=f.bytes_total,
                created_at=f.created_at,
            )
            for f in files
        ],
    )
