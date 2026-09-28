"""Arq worker: time-window meeting digest generation — v3.18.3.

`GET /api/digests` used to await `_generate_digest()` inline, which
does up to 11 LLM calls (10 mini-summaries + 1 synthesis). On real
catalogs that's a 60-180 second wait that consistently exceeds CF's
edge timeout. This worker runs the same logic out-of-band; the HTTP
endpoint returns 202 + job_id and the frontend polls.

Idempotency: the worker upserts on `(org_id, period, date)` (same as
the pre-existing cached-digest pattern) so two enqueues of the same
window converge to one final cached row.

Drift safety: at entry the worker checks the cached digest's
`generation_job_id`. If it doesn't match our ctx job_id, a newer job
is processing the same window — bail without writing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def generate_digest_job(
    ctx: dict[str, Any],
    org_id: int,
    period: str,
    date_str: str,
    project_id: str | int | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Generate a digest off the request path.

    Returns a payload matching the legacy `DigestResponse` so the
    frontend can render it directly from the job result.
    """
    from middleware.request_context import bind_request_id
    bind_request_id(request_id or ctx.get("job_id"))
    from database.database import SessionLocal
    from database.models import MeetingDigest

    own_job_id = ctx.get("job_id") if isinstance(ctx, dict) else None

    db = SessionLocal()
    try:
        # Drift check: if a row exists with a different generation_job_id,
        # a newer job is processing this window. Bail so we don't overwrite.
        existing = (
            db.query(MeetingDigest)
            .filter(
                MeetingDigest.organization_id == org_id,
                MeetingDigest.period == period,
                MeetingDigest.date == date_str,
            )
            .first()
        )
        if existing and own_job_id and existing.generation_job_id and existing.generation_job_id != own_job_id:
            logger.warning(
                "generate_digest_job: drift org=%s window=%s/%s own=%s current=%s — skipping",
                org_id, period, date_str, own_job_id, existing.generation_job_id,
            )
            return {
                "status": "skipped",
                "reason": "drift",
                "org_id": org_id,
                "period": period,
                "date": date_str,
            }

        # Delegate to the existing implementation. We import here (not at
        # module top) so the worker module doesn't drag the digests API
        # router into the import graph for callers that only need the
        # worker registration.
        from api.digests import _generate_digest

        # _generate_digest expects project_id as Optional[int]; tolerate
        # string for safe transport over arq (JSON-encoded args).
        pid: int | None
        if project_id is None or project_id == "":
            pid = None
        else:
            try:
                pid = int(project_id)
            except (TypeError, ValueError):
                pid = None

        result = await _generate_digest(db, org_id, period, date_str, pid)

        # Stamp the cached row with this job_id so a future enqueue can
        # drift-check against it.
        cached = (
            db.query(MeetingDigest)
            .filter(
                MeetingDigest.organization_id == org_id,
                MeetingDigest.period == period,
                MeetingDigest.date == date_str,
            )
            .first()
        )
        if cached and own_job_id:
            cached.generation_job_id = own_job_id
            db.commit()

        payload = result.model_dump() if hasattr(result, "model_dump") else dict(result)
        payload["status"] = "completed"
        return payload
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "generate_digest_job: crashed org=%s window=%s/%s", org_id, period, date_str,
        )
        return {
            "status": "failed",
            "org_id": org_id,
            "period": period,
            "date": date_str,
            "error": str(exc)[:500],
        }
    finally:
        db.close()
