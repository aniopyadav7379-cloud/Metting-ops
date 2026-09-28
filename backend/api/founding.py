"""Founding 100 cohort status endpoints (v3.21.0).

Two routes:

  GET  /api/founding/status?cohort=meeting_ops_v1
      Public (no auth), 60-second cached. Returns the current cohort
      capacity + the count of seats already taken so the public
      /founding-100 marketing page can render "X / 100 seats filled".
      Body: {cohort, seats_taken, seats_total, is_open}

  POST /api/admin/founding/close
      Admin-only. Flips a process-local "closed" flag for the cohort so
      future Stripe webhook events stop granting the founding-member flag
      even if the cohort is nominally below capacity. Aaron's hard stop
      for "we decided to stop letting people in early."
      Body: {"cohort": "meeting_ops_v1"}

The status route caches its DB count in-process for 60 seconds. That's
cheap to recompute (one indexed COUNT) but the landing page can hit it
hard, so we still skip a round-trip the bulk of the time. A multi-pod
deploy will surface up to 60 seconds of skew between pods, which is
fine for a marketing counter.

The "closed" flag lives in process memory + a Redis key when available
so it propagates across pods. Restart-persistence is intentional:
restarting a pod after the flag is set should NOT silently re-open the
cohort. Operators can re-open by clearing the Redis key + setting
FOUNDING_COHORT_CLOSED= unset / false.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import require_admin
from auth.models import User
from database.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/founding", tags=["founding"])
admin_router = APIRouter(prefix="/api/admin/founding", tags=["founding"])


_FOUNDING_DEFAULT_COHORT = "meeting_ops_v1"
_STATUS_CACHE_TTL_SECONDS = 60
_status_cache: dict[str, tuple[float, dict]] = {}


def _cohort_capacity() -> int:
    raw = os.getenv("FOUNDING_100_LIMIT") or os.getenv("FOUNDERS_100_LIMIT") or "100"
    try:
        return int(raw)
    except (ValueError, TypeError):
        return 100


def _closed_via_env() -> bool:
    """Env-level kill switch — survives restarts, takes precedence."""
    raw = os.getenv("FOUNDING_COHORT_CLOSED", "").strip().lower()
    return raw in ("1", "true", "yes", "on")


# Process-local closed set; written by POST /api/admin/founding/close.
# Augmented by a Redis check below when available so a multi-pod deploy
# converges on the close decision within seconds.
_closed_cohorts_local: set[str] = set()


def _redis_client():
    try:
        import redis  # type: ignore
    except ImportError:
        return None
    url = os.getenv("REDIS_URL", "").strip()
    if not url:
        return None
    try:
        return redis.from_url(
            url, decode_responses=True,
            socket_connect_timeout=1, socket_timeout=1,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("founding: redis unavailable: %s", exc)
        return None


def _redis_key_closed(cohort: str) -> str:
    return f"founding:cohort:closed:{cohort}"


def _is_cohort_closed(cohort: str) -> bool:
    """True if the cohort is no longer accepting new seats (admin-closed
    or env-killed). Order: env > Redis > in-proc set."""
    if _closed_via_env():
        return True
    if cohort in _closed_cohorts_local:
        return True
    client = _redis_client()
    if client is None:
        return False
    try:
        return bool(client.get(_redis_key_closed(cohort)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("founding: redis closed-check failed: %s", exc)
        return False


def _set_cohort_closed(cohort: str) -> None:
    """Mark the cohort closed locally + in Redis (if available). Idempotent."""
    _closed_cohorts_local.add(cohort)
    client = _redis_client()
    if client is None:
        return
    try:
        # No TTL — close decisions are sticky on purpose.
        client.set(_redis_key_closed(cohort), "1")
    except Exception as exc:  # noqa: BLE001
        logger.warning("founding: redis closed-set failed: %s", exc)


# ---------------------------------------------------------------------------
# Public status
# ---------------------------------------------------------------------------


class FoundingStatusResponse(BaseModel):
    cohort: str
    seats_taken: int
    seats_total: int
    is_open: bool


def _count_seats(db: Session, cohort: str) -> int:
    return (
        db.query(User)
        .filter(User.is_founding_member.is_(True))
        .filter(User.founding_cohort == cohort)
        .count()
    )


@router.get("/status", response_model=FoundingStatusResponse)
async def founding_status(
    cohort: str = _FOUNDING_DEFAULT_COHORT,
    db: Session = Depends(get_db),
) -> FoundingStatusResponse:
    """Public cohort status — used by the /founding-100 landing page.

    No auth. Cached 60s in-process to absorb landing-page bursts. The
    response is a tight {cohort, seats_taken, seats_total, is_open} so
    no PII ever leaks (no user IDs, no emails)."""
    now = time.time()
    cached = _status_cache.get(cohort)
    if cached is not None and now - cached[0] < _STATUS_CACHE_TTL_SECONDS:
        return FoundingStatusResponse(**cached[1])

    seats_total = _cohort_capacity()
    seats_taken = _count_seats(db, cohort)
    is_open = (not _is_cohort_closed(cohort)) and seats_taken < seats_total

    body = {
        "cohort": cohort,
        "seats_taken": seats_taken,
        "seats_total": seats_total,
        "is_open": is_open,
    }
    _status_cache[cohort] = (now, body)
    return FoundingStatusResponse(**body)


# ---------------------------------------------------------------------------
# Admin close
# ---------------------------------------------------------------------------


class FoundingCloseRequest(BaseModel):
    cohort: str


class FoundingCloseResponse(BaseModel):
    cohort: str
    closed: bool


@admin_router.post("/close", response_model=FoundingCloseResponse)
async def founding_close(
    payload: FoundingCloseRequest,
    current_user: User = Depends(require_admin),
) -> FoundingCloseResponse:
    """Admin: stop granting new seats in the named cohort.

    Sticky — restart-safe via Redis. Future Stripe webhooks for
    annual-upfront subscriptions will skip the grant for this cohort.
    The /api/founding/status endpoint will report is_open=false once the
    60s status cache rolls over (cohort-local clear so the bust is fast)."""
    cohort = (payload.cohort or "").strip()
    if not cohort:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cohort is required",
        )
    _set_cohort_closed(cohort)
    # Bust the status cache so the landing page flips to "closed" promptly.
    _status_cache.pop(cohort, None)
    logger.info(
        "founding_cohort_closed cohort=%s closed_by=%s",
        cohort, current_user.id,
    )
    return FoundingCloseResponse(cohort=cohort, closed=True)
