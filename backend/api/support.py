"""Customer support contact-form backend (v3.21.0).

Single endpoint:

    POST /api/support/contact   body: {name, email, subject, message}

Behavior mirrors `backend/api/landing.py`:

    - Public (no auth required) — but accepts an authenticated caller
      and stamps `user_id` on the row when present so the support
      inbox can correlate to known users.
    - Rate-limited per-email (3 / hour). Redis SETEX canonical with a
      per-process fallback bucket.
    - Best-effort Postmark notify to `SUPPORT_NOTIFY_EMAIL`
      (default `support@magicunicorn.tech`). Mail failures are logged
      but the request still succeeds — the row landed, triage is from
      the table.

This mirrors landing.py's structure intentionally so future changes to
either (e.g. swapping to a queue) can be made in lockstep.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user_optional
from auth.models import User
from database.database import get_db
from database.models import SupportRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/support", tags=["support"])


# --------------------------------------------------------------------------- #
# Rate limiter — Redis preferred, in-process fallback. Per-email key.         #
# --------------------------------------------------------------------------- #

_RATE_LIMIT_MAX = 3
_RATE_LIMIT_WINDOW_SECONDS = 60 * 60  # 1 hour
_INPROC_BUCKET: dict[str, list[float]] = {}
_REDIS_WARNED = False


def _inproc_under_limit(key: str) -> bool:
    now = time.time()
    bucket = [t for t in _INPROC_BUCKET.get(key, []) if now - t < _RATE_LIMIT_WINDOW_SECONDS]
    if len(bucket) >= _RATE_LIMIT_MAX:
        _INPROC_BUCKET[key] = bucket
        return False
    bucket.append(now)
    _INPROC_BUCKET[key] = bucket
    return True


async def _redis_under_limit(key: str) -> Optional[bool]:
    global _REDIS_WARNED
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None
    try:
        import redis.asyncio as redis  # local import
    except Exception as exc:  # noqa: BLE001
        if not _REDIS_WARNED:
            logger.warning("support rate-limit: redis lib unavailable (%s); using in-proc fallback", exc)
            _REDIS_WARNED = True
        return None

    rkey = f"support:contact:rl:{key}"
    try:
        client = await redis.from_url(redis_url, decode_responses=True)
        try:
            count = await client.incr(rkey)
            if count == 1:
                await client.expire(rkey, _RATE_LIMIT_WINDOW_SECONDS)
            return count <= _RATE_LIMIT_MAX
        finally:
            await client.close()
    except Exception as exc:  # noqa: BLE001
        if not _REDIS_WARNED:
            logger.warning("support rate-limit: redis unreachable (%s); using in-proc fallback", exc)
            _REDIS_WARNED = True
        return None


async def _under_rate_limit(email: str) -> bool:
    redis_result = await _redis_under_limit(email)
    if redis_result is not None:
        return redis_result
    return _inproc_under_limit(email)


# --------------------------------------------------------------------------- #
# Notification — Postmark, soft-fail.                                         #
# --------------------------------------------------------------------------- #

_POSTMARK_TOKEN_ENVS = ("POSTMARK_API_TOKEN", "POSTMARK_SERVER_TOKEN")
_SUPPORT_NOTIFY_TO = os.getenv("SUPPORT_NOTIFY_EMAIL", "support@magicunicorn.tech")


def _postmark_token() -> str:
    for name in _POSTMARK_TOKEN_ENVS:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _notify_support_request(
    name: str, email: str, subject: str, message: str, user_id: Optional[int]
) -> None:
    """Send the support inbox a heads-up email. Soft-fail."""
    token = _postmark_token()
    if not token:
        logger.info(
            "support contact: postmark not configured, skipping notify (email=%s)", email
        )
        return
    sender = os.getenv("POSTMARK_FROM", "").strip()
    if not sender:
        logger.warning("support contact: POSTMARK_FROM unset; cannot send notify")
        return
    stream = os.getenv("POSTMARK_MESSAGE_STREAM", "outbound").strip() or "outbound"

    body_lines = [
        f"New customer support request.",
        "",
        f"From: {name or '(no name)'} <{email}>",
        f"User ID: {user_id if user_id is not None else '(anonymous)'}",
        f"Subject: {subject}",
        "",
        "Message:",
        message,
        "",
        "Reply directly to the From address; row archived in support_requests.",
    ]
    text_body = "\n".join(body_lines)

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
                    "To": _SUPPORT_NOTIFY_TO,
                    "ReplyTo": email,
                    "Subject": f"[Support] {subject}",
                    "TextBody": text_body,
                    "MessageStream": stream,
                },
            )
        if resp.status_code != 200:
            logger.warning(
                "support contact: postmark non-200 (%s) for %s", resp.status_code, email
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("support contact: postmark send failed: %s", exc)


# --------------------------------------------------------------------------- #
# Request/response models.                                                    #
# --------------------------------------------------------------------------- #

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MAX_NAME = 200
_MAX_SUBJECT = 200
_MAX_MESSAGE = 10000


class ContactBody(BaseModel):
    name: str = ""
    email: str
    subject: str
    message: str


class ContactResponse(BaseModel):
    ok: bool


# --------------------------------------------------------------------------- #
# Endpoint.                                                                   #
# --------------------------------------------------------------------------- #


@router.post(
    "/contact",
    response_model=ContactResponse,
    status_code=status.HTTP_200_OK,
)
async def submit_support_request(
    body: ContactBody,
    request: Request,  # noqa: ARG001 — kept for symmetry with landing.py + future IP rate-limit
    db: Session = Depends(get_db),
    caller: Optional[User] = Depends(get_current_user_optional),
) -> ContactResponse:
    """Public + authed contact form. Rate-limited per email."""

    email = (body.email or "").strip().lower()
    if not email or not _EMAIL_RE.match(email) or len(email) > 320:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please enter a valid email address.",
        )

    name = (body.name or "").strip()[:_MAX_NAME]
    subject = (body.subject or "").strip()
    message = (body.message or "").strip()

    if not subject:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Subject is required.",
        )
    if not message:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message is required.",
        )
    if len(subject) > _MAX_SUBJECT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Subject must be {_MAX_SUBJECT} characters or fewer.",
        )
    if len(message) > _MAX_MESSAGE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Message must be {_MAX_MESSAGE} characters or fewer.",
        )

    if not await _under_rate_limit(email):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many support requests from this email. Try again in an hour.",
        )

    user_id = caller.id if caller is not None else None
    row = SupportRequest(
        email=email,
        name=name or None,
        subject=subject,
        message=message,
        user_id=user_id,
    )
    db.add(row)
    db.commit()

    logger.info(
        "support contact: new (email=%s user_id=%s subject=%r)",
        email,
        user_id,
        subject[:80],
    )
    _notify_support_request(name, email, subject, message, user_id)
    return ContactResponse(ok=True)
