"""Per-org upload quotas.

Tier defaults live here as the source of truth. Per-org overrides on the
Organization row (max_file_bytes, max_concurrent_uploads, max_monthly_hours)
take precedence when set. Quota enforcement raises HTTPException with a
structured detail payload so the frontend can show a useful upsell.

Lago integration is intentionally not wired here yet. The plan column on
Organization is the trust source for tier; a Lago webhook will write to it
in a follow-up.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, TypedDict

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from auth.models import Organization
from database.models import RecordingSession, UploadJob


class TierLimits(TypedDict):
    max_file_bytes: int
    max_concurrent_uploads: int
    max_monthly_hours: Optional[int]  # None = unlimited


# Source of truth for tier defaults. Per-org column overrides win when set.
#
# IMPORTANT: every paid tier MUST have an entry here. A missing key falls through
# to "free" (see get_org_limits), which previously meant a $7.99 Sync/$35 Suite
# org silently got Free's 10h/500MB/1-concurrent caps — a $35 Suite customer was
# *more* restricted than a $20 Pro. (Fixed: sync/basic/suite added below.)
TIER_DEFAULTS: dict[str, TierLimits] = {
    "free": {
        "max_file_bytes": 500 * 1024 * 1024,           # 500 MB
        "max_concurrent_uploads": 1,
        "max_monthly_hours": 10,
    },
    # "sync" (formerly "basic", $7.99): server-side TEXT sync / search / chat only
    # — NO server audio upload (gated off in auth/tier.py: audio_upload=False), so
    # these upload caps are belt-and-suspenders and never bind in practice. Set to
    # free-equivalent so a Sync org never silently inherits another tier's caps.
    "sync": {
        "max_file_bytes": 500 * 1024 * 1024,           # 500 MB
        "max_concurrent_uploads": 1,
        "max_monthly_hours": 10,
    },
    # "basic" — permanent alias of "sync" for pre-rename orgs (plan="basic" rows).
    "basic": {
        "max_file_bytes": 500 * 1024 * 1024,           # 500 MB
        "max_concurrent_uploads": 1,
        "max_monthly_hours": 10,
    },
    "pro": {
        "max_file_bytes": 2 * 1024 * 1024 * 1024,      # 2 GB
        "max_concurrent_uploads": 4,
        "max_monthly_hours": 100,
    },
    # "suite" ($35): Pro + cross-app (UC) entitlement. MUST be >= Pro (the bug was
    # Suite falling through to Free). Premium audio budget, below Enterprise's
    # unlimited. NOTE(Aaron): sensible defaults — confirm/adjust the exact numbers.
    "suite": {
        "max_file_bytes": 5 * 1024 * 1024 * 1024,      # 5 GB
        "max_concurrent_uploads": 8,
        "max_monthly_hours": 250,
    },
    "enterprise": {
        "max_file_bytes": 10 * 1024 * 1024 * 1024,     # 10 GB
        "max_concurrent_uploads": 16,
        "max_monthly_hours": None,
    },
}

# Pipeline stages that count as "in flight" for the concurrent uploads cap.
INFLIGHT_STAGES = (
    "uploading",
    "queued",
    "extracting",
    "transcribing",
    "diarizing",
    "summarizing",
    "embedding",
)


class EffectiveLimits(TypedDict):
    plan: str
    max_file_bytes: int
    max_concurrent_uploads: int
    max_monthly_hours: Optional[int]


def get_org_limits(org: Organization) -> EffectiveLimits:
    """Resolve effective limits for an org: per-org override > tier default."""
    plan = (org.plan or "free").lower()
    defaults = TIER_DEFAULTS.get(plan, TIER_DEFAULTS["free"])
    return {
        "plan": plan,
        "max_file_bytes": int(org.max_file_bytes) if org.max_file_bytes else defaults["max_file_bytes"],
        "max_concurrent_uploads": (
            int(org.max_concurrent_uploads) if org.max_concurrent_uploads else defaults["max_concurrent_uploads"]
        ),
        "max_monthly_hours": (
            int(org.max_monthly_hours) if org.max_monthly_hours else defaults["max_monthly_hours"]
        ),
    }


def count_inflight_uploads(db: Session, org_id: int) -> int:
    """Count upload jobs currently in any pre-completion pipeline stage."""
    return (
        db.query(func.count(UploadJob.id))
        .filter(
            UploadJob.organization_id == org_id,
            UploadJob.stage.in_(INFLIGHT_STAGES),
        )
        .scalar()
        or 0
    )


def sum_monthly_audio_seconds(db: Session, org_id: int) -> float:
    """Sum recording_sessions.duration for the org over the current calendar
    month (UTC). Returns seconds (float). Includes all sessions regardless
    of stage so a session that's still transcribing already counts."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    total = (
        db.query(func.coalesce(func.sum(RecordingSession.duration), 0.0))
        .filter(
            RecordingSession.organization_id == org_id,
            RecordingSession.created_at >= month_start,
        )
        .scalar()
    )
    return float(total or 0.0)


class QuotaUsage(TypedDict):
    plan: str
    inflight_uploads: int
    inflight_uploads_limit: int
    monthly_audio_hours: float
    monthly_audio_hours_limit: Optional[float]
    max_file_bytes: int


def get_usage(db: Session, org: Organization) -> QuotaUsage:
    """Snapshot of current usage + applicable limits for an org."""
    limits = get_org_limits(org)
    monthly_seconds = sum_monthly_audio_seconds(db, org.id)
    return {
        "plan": limits["plan"],
        "inflight_uploads": count_inflight_uploads(db, org.id),
        "inflight_uploads_limit": limits["max_concurrent_uploads"],
        "monthly_audio_hours": round(monthly_seconds / 3600.0, 2),
        "monthly_audio_hours_limit": (
            float(limits["max_monthly_hours"]) if limits["max_monthly_hours"] is not None else None
        ),
        "max_file_bytes": limits["max_file_bytes"],
    }


def check_upload_quota(db: Session, org: Organization, file_size: int) -> None:
    """Enforce upload-time quotas. Raises HTTPException(402) with a structured
    detail payload that the frontend can read to show the right upsell.

    Order of checks:
        1. file size cap (cheapest, fail fastest)
        2. concurrent in-flight uploads cap
        3. monthly audio hours cap (only known after extraction; this is a
           conservative pre-check assuming the upload itself is the audio)
    """
    limits = get_org_limits(org)

    if file_size > limits["max_file_bytes"]:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "file_too_large",
                "message": (
                    f"File size {file_size / 1024 / 1024:.1f} MB exceeds the "
                    f"{limits['max_file_bytes'] / 1024 / 1024:.0f} MB limit on "
                    f"the {limits['plan']} plan."
                ),
                "plan": limits["plan"],
                "limit_bytes": limits["max_file_bytes"],
                "requested_bytes": file_size,
            },
        )

    inflight = count_inflight_uploads(db, org.id)
    if inflight >= limits["max_concurrent_uploads"]:
        raise HTTPException(
            status_code=429,
            detail={
                "code": "concurrent_uploads_exceeded",
                "message": (
                    f"You have {inflight} upload(s) in progress; the "
                    f"{limits['plan']} plan allows {limits['max_concurrent_uploads']} "
                    "concurrent. Wait for an active upload to finish."
                ),
                "plan": limits["plan"],
                "inflight": inflight,
                "limit": limits["max_concurrent_uploads"],
            },
        )

    if limits["max_monthly_hours"] is not None:
        monthly_seconds = sum_monthly_audio_seconds(db, org.id)
        monthly_hours = monthly_seconds / 3600.0
        if monthly_hours >= limits["max_monthly_hours"]:
            raise HTTPException(
                status_code=402,
                detail={
                    "code": "monthly_hours_exceeded",
                    "message": (
                        f"You've used {monthly_hours:.1f} of "
                        f"{limits['max_monthly_hours']} monthly audio hours on "
                        f"the {limits['plan']} plan. Upgrade or wait until next month."
                    ),
                    "plan": limits["plan"],
                    "used_hours": round(monthly_hours, 2),
                    "limit_hours": float(limits["max_monthly_hours"]),
                },
            )
