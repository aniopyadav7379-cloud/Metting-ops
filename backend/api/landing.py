"""Public, invite-only landing page backend.

Powers the v3.19 polished launch landing page at
`meeting-ops.unicorncommander.ai/`. Single endpoint:

    POST /api/landing/invite-request   body: {"email": "..."}

Behavior:

    - Public (no auth, no oauth2-proxy bypass needed because there is
      no caller identity to bypass).
    - Idempotent on duplicate emails — second POST returns 200 with the
      same `{"ok": true}` body and does NOT re-send a notification.
    - Rate-limited per-IP (5 / hour). Uses Redis SETEX as the canonical
      counter; falls back to a small in-process dict (per-pod) with a
      warning if Redis is unreachable. Either path returns HTTP 429
      with a friendly body when the cap is hit.
    - Optionally emails `aaron@magicunicorn.tech` via Postmark when
      `POSTMARK_API_TOKEN` (or the existing `POSTMARK_SERVER_TOKEN`) is
      configured. Without a token the endpoint logs the request and
      returns success — we never block on the notification path.

This module deliberately has zero dependencies on the rest of the auth
stack (no `auth.routes` import, no `Depends(get_current_user)`). Adding
those would risk an accidental gate on the landing page and break the
"no API call until the user clicks" contract.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.database import get_db
from database.models import InviteRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/landing", tags=["landing"])


# --------------------------------------------------------------------------- #
# Rate limiter — Redis preferred, in-process fallback.                        #
# --------------------------------------------------------------------------- #

_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 hour
_INPROC_BUCKET: dict[str, list[float]] = {}
_REDIS_WARNED = False


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Trusts X-Forwarded-For when set by our
    own Traefik / oauth2-proxy stack, otherwise falls back to the
    direct socket peer. The landing page sits behind Traefik in prod
    so XFF is the canonical source."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        # First entry is the original client; the rest are proxies.
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _inproc_under_limit(ip: str) -> bool:
    """Per-process fallback used when Redis is unreachable. Not safe
    across pods; that's the warning we log. Good enough to keep a
    single rogue script from filling the table."""
    now = time.time()
    bucket = [t for t in _INPROC_BUCKET.get(ip, []) if now - t < _RATE_LIMIT_WINDOW_SECONDS]
    if len(bucket) >= _RATE_LIMIT_MAX:
        _INPROC_BUCKET[ip] = bucket
        return False
    bucket.append(now)
    _INPROC_BUCKET[ip] = bucket
    return True


async def _redis_under_limit(ip: str) -> Optional[bool]:
    """Returns True if under limit, False if over, None if Redis
    unavailable (caller should fall back to the in-proc bucket)."""
    global _REDIS_WARNED
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None
    try:
        import redis.asyncio as redis  # local import keeps this module
                                        # importable in environments
                                        # without redis installed
    except Exception as exc:  # noqa: BLE001
        if not _REDIS_WARNED:
            logger.warning("landing rate-limit: redis lib unavailable (%s); using in-proc fallback", exc)
            _REDIS_WARNED = True
        return None

    key = f"landing:invite:rl:{ip}"
    try:
        client = await redis.from_url(redis_url, decode_responses=True)
        try:
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
            return count <= _RATE_LIMIT_MAX
        finally:
            await client.close()
    except Exception as exc:  # noqa: BLE001
        if not _REDIS_WARNED:
            logger.warning("landing rate-limit: redis unreachable (%s); using in-proc fallback", exc)
            _REDIS_WARNED = True
        return None


async def _under_rate_limit(ip: str) -> bool:
    redis_result = await _redis_under_limit(ip)
    if redis_result is not None:
        return redis_result
    return _inproc_under_limit(ip)


# --------------------------------------------------------------------------- #
# Notification — Postmark, soft-fail.                                         #
# --------------------------------------------------------------------------- #

# Spec name from the task. Falls back to the existing repo-wide
# POSTMARK_SERVER_TOKEN env var so we don't need to set two secrets on
# the same machine.
_POSTMARK_TOKEN_ENVS = ("POSTMARK_API_TOKEN", "POSTMARK_SERVER_TOKEN")
_INVITE_NOTIFY_TO = os.getenv("INVITE_NOTIFY_EMAIL", "aaron@magicunicorn.tech")


def _postmark_token() -> str:
    for name in _POSTMARK_TOKEN_ENVS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _notify_invite_request(email: str) -> None:
    """Send Aaron a heads-up email when an invite is requested.
    Soft-fail: errors are logged and swallowed so the public endpoint
    always reports success when the row landed in the DB."""
    token = _postmark_token()
    if not token:
        logger.info("landing invite-request: postmark not configured, skipping notify (email=%s)", email)
        return
    sender = os.getenv("POSTMARK_FROM", "").strip()
    if not sender:
        logger.warning("landing invite-request: POSTMARK_FROM unset; cannot send notify")
        return
    stream = os.getenv("POSTMARK_MESSAGE_STREAM", "outbound").strip() or "outbound"

    try:
        import httpx
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                "https://api.postmarkapp.com/email",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Postmark-Server-Token": token,
                },
                json={
                    "From": sender,
                    "To": _INVITE_NOTIFY_TO,
                    "Subject": f"New invite request: {email}",
                    "TextBody": (
                        f"A new invite request landed on the public landing page.\n\n"
                        f"Email: {email}\n\n"
                        f"Triage in the invite_requests table (set processed_at when sent)."
                    ),
                    "MessageStream": stream,
                },
            )
        if resp.status_code != 200:
            logger.warning("landing invite-request: postmark non-200 (%s) for %s", resp.status_code, email)
    except Exception as exc:  # noqa: BLE001
        logger.warning("landing invite-request: postmark send failed: %s", exc)


# --------------------------------------------------------------------------- #
# Request/response models.                                                    #
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class InviteRequestBody(BaseModel):
    email: str


class InviteRequestResponse(BaseModel):
    ok: bool


# --------------------------------------------------------------------------- #
# Endpoint.                                                                   #
# --------------------------------------------------------------------------- #

@router.post(
    "/invite-request",
    response_model=InviteRequestResponse,
    status_code=status.HTTP_200_OK,
)
async def submit_invite_request(
    body: InviteRequestBody,
    request: Request,
    db: Session = Depends(get_db),
) -> InviteRequestResponse:
    """Accept a public invite request. Idempotent + rate-limited."""

    email = (body.email or "").strip().lower()
    if not email or not _EMAIL_RE.match(email) or len(email) > 320:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter a valid email address.",
        )

    ip = _client_ip(request)
    if not await _under_rate_limit(ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Try again in an hour.",
        )

    # Idempotent insert: check first, fall back to integrity-error swallow
    # in case of a race between the SELECT and the INSERT.
    existing = db.execute(
        select(InviteRequest).where(InviteRequest.email == email)
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("landing invite-request: duplicate, no notify (email=%s)", email)
        return InviteRequestResponse(ok=True)

    row = InviteRequest(email=email)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Concurrent insert; treat as duplicate.
        logger.info("landing invite-request: race-detected duplicate (email=%s)", email)
        return InviteRequestResponse(ok=True)

    logger.info("landing invite-request: new (email=%s)", email)
    _notify_invite_request(email)
    return InviteRequestResponse(ok=True)
