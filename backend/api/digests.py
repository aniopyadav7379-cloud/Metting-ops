"""
Time-Window Digests API — map-reduce meeting summaries over time periods.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone, timedelta
import logging

from database.database import get_db
from database.models import RecordingSession, MeetingDigest
from auth.dependencies import get_current_organization, get_current_user
from auth.organization import ActiveOrganization
from auth.models import User
from auth.tier import gate_feature_for_caller
from services.providers.registry import get_provider_registry

router = APIRouter(prefix="/api/digests", tags=["Digests"])
logger = logging.getLogger(__name__)


class DigestResponse(BaseModel):
    id: Optional[int] = None
    period: str
    date: str
    project_id: Optional[int] = None
    content: str
    meeting_count: int
    cached: bool = False


class DigestJobResponse(BaseModel):
    """202 response: digest generation is queued; poll /api/jobs/{job_id}."""
    job_id: str
    status_url: str
    period: str
    date: str
    cached: bool = False


def _get_date_range(period: str, date_str: str) -> tuple[datetime, datetime]:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if period == "day":
        return dt, dt + timedelta(days=1)
    elif period == "week":
        start = dt - timedelta(days=dt.weekday())
        return start, start + timedelta(days=7)
    elif period == "month":
        start = dt.replace(day=1)
        if dt.month == 12:
            end = dt.replace(year=dt.year + 1, month=1, day=1)
        else:
            end = dt.replace(month=dt.month + 1, day=1)
        return start, end
    elif period == "quarter":
        quarter_start_month = ((dt.month - 1) // 3) * 3 + 1
        start = dt.replace(month=quarter_start_month, day=1)
        if quarter_start_month + 3 > 12:
            end = dt.replace(year=dt.year + 1, month=1, day=1)
        else:
            end = dt.replace(month=quarter_start_month + 3, day=1)
        return start, end
    elif period == "year":
        start = dt.replace(month=1, day=1)
        end = dt.replace(year=dt.year + 1, month=1, day=1)
        return start, end
    else:
        raise ValueError(f"Invalid period: {period}")


async def _generate_digest(
    db: Session,
    org_id: int,
    period: str,
    date_str: str,
    project_id: Optional[int],
) -> DigestResponse:
    start_dt, end_dt = _get_date_range(period, date_str)
    query = db.query(RecordingSession).filter(
        RecordingSession.organization_id == org_id,
        RecordingSession.created_at >= start_dt,
        RecordingSession.created_at < end_dt,
        RecordingSession.status.in_(["completed"]),
    )
    if project_id is not None:
        query = query.filter(RecordingSession.project_id == project_id)

    sessions = query.order_by(RecordingSession.created_at.asc()).all()

    if not sessions:
        return DigestResponse(
            period=period,
            date=date_str,
            project_id=project_id,
            content=f"No completed meetings found for {period} {date_str}.",
            meeting_count=0,
            cached=True,
        )

    meeting_texts = []
    for s in sessions:
        title = s.title or s.name or "Untitled"
        summary = s.summary or "No summary available."
        meeting_texts.append(f"Meeting: {title}\nSummary: {summary}")

    combined = "\n\n---\n\n".join(meeting_texts)

    registry = get_provider_registry(db)
    llm = registry.get_llm(org_id, task="quality")

    chunks = []
    chunk_size = 4000
    for i in range(0, len(combined), chunk_size):
        chunks.append(combined[i:i + chunk_size])

    mini_summaries = []
    for chunk in chunks[:10]:
        prompt = f"Summarize these meeting notes in 2-3 sentences:\n\n{chunk}"
        result = await llm.chat(
            system_prompt="You are a meeting analyst. Summarize concisely.",
            user_prompt=prompt,
            max_tokens=200,
            temperature=0.3,
        )
        if result:
            mini_summaries.append(result)

    if len(mini_summaries) > 1:
        synthesis = await llm.chat(
            system_prompt="You synthesize multiple meeting summaries into a cohesive digest.",
            user_prompt=f"Synthesize these meeting summaries into a unified {period} digest:\n\n" + "\n".join(mini_summaries),
            max_tokens=800,
            temperature=0.3,
        )
        content = synthesis or "\n".join(mini_summaries)
    elif mini_summaries:
        content = mini_summaries[0]
    else:
        content = f"Could not generate digest for {period} {date_str}."

    existing = (
        db.query(MeetingDigest)
        .filter(
            MeetingDigest.organization_id == org_id,
            MeetingDigest.period == period,
            MeetingDigest.date == date_str,
        )
        .first()
    )

    if existing:
        existing.content = content
        existing.meeting_count = len(sessions)
        db.commit()
        db.refresh(existing)
        return DigestResponse(
            id=existing.id,
            period=period,
            date=date_str,
            project_id=project_id,
            content=content,
            meeting_count=len(sessions),
            cached=True,
        )

    digest = MeetingDigest(
        organization_id=org_id,
        period=period,
        date=date_str,
        project_id=project_id,
        content=content,
        meeting_count=len(sessions),
    )
    db.add(digest)
    db.commit()
    db.refresh(digest)
    return DigestResponse(
        id=digest.id,
        period=period,
        date=date_str,
        project_id=project_id,
        content=content,
        meeting_count=len(sessions),
        cached=True,
    )


@router.get("")
async def get_digest(
    response: Response,
    period: str = Query(..., description="day, week, month, quarter, year"),
    date: str = Query(None, description="YYYY-MM-DD (defaults to today)"),
    project_id: Optional[int] = Query(None),
    force: bool = Query(False, description="Force regeneration"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """Fetch a time-window digest.

    Behavior:
      - Cached digest exists AND `force=false`: return 200 with the cached
        content (any tier).
      - No cache OR `force=true`: tier-gate, then enqueue an arq job and
        return 202 with `{job_id, status_url}`. The frontend polls
        `GET /api/jobs/{job_id}` until the worker finishes.

    v3.18.3 background-jobification: the inline path used to do up to 11
    LLM calls (10 mini-summaries + 1 synthesis), routinely exceeding
    Cloudflare's edge timeout. Heavy work now runs out-of-band.
    """
    if period not in ("day", "week", "month", "quarter", "year"):
        raise HTTPException(status_code=400, detail="Invalid period")
    # Default date to today if not provided
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")

    if not force:
        existing = (
            db.query(MeetingDigest)
            .filter(
                MeetingDigest.organization_id == active_org.organization.id,
                MeetingDigest.period == period,
                MeetingDigest.date == date,
            )
            .first()
        )
        if existing:
            return DigestResponse(
                id=existing.id,
                period=period,
                date=date,
                project_id=project_id,
                content=existing.content,
                meeting_count=existing.meeting_count,
                cached=True,
            )

    # v3.18.2 tier gate: digest generation runs the server LLM (multiple
    # chat calls to mini-summarize + synthesize). Cached digests above
    # are free for any tier to read; new generation is paid-tier.
    gate_feature_for_caller(current_user, "canonical_reprocess", active_org)

    # v3.18.3: enqueue + 202 + poll. Fall back to inline if arq is
    # unavailable so dev/test envs without Redis keep working.
    try:
        from services.job_runner import enqueue_job
        job_id = await enqueue_job(
            "generate_digest_job",
            active_org.organization.id,
            period,
            date,
            project_id=str(project_id) if project_id is not None else None,
            owner_user_id=current_user.id,
            owner_org_id=active_org.organization.id,
        )
        # Stamp the digest row if one already exists so future workers
        # can drift-check.
        existing_row = (
            db.query(MeetingDigest)
            .filter(
                MeetingDigest.organization_id == active_org.organization.id,
                MeetingDigest.period == period,
                MeetingDigest.date == date,
            )
            .first()
        )
        if existing_row is not None:
            existing_row.generation_job_id = job_id
            db.commit()
        response.status_code = 202
        return DigestJobResponse(
            job_id=job_id,
            status_url=f"/api/jobs/{job_id}",
            period=period,
            date=date,
            cached=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "get_digest: arq enqueue failed period=%s date=%s: %s — running inline",
            period, date, exc,
        )

    return await _generate_digest(db, active_org.organization.id, period, date, project_id)
