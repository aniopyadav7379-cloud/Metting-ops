"""Google Calendar integration (Phase 3F).

Audit finding: ZIP2 (meeting-flow) has a real, correct Google OAuth2
authorization-code + refresh-token flow (src/lib/integrations/google/
oauth.ts) — auth URL construction, code exchange, token refresh, userinfo
lookup. That flow's MECHANICS are ported here (same endpoints, same grant
types, same token-handling shape). What is NOT reused as-is: ZIP2's scope
is `calendar.readonly` (it only reads calendar events to detect meetings).
This product's Phase 3F requirement is the opposite direction — creating
calendar events FROM action items — which needs write access
(`calendar.events`), so the scope was changed; everything else about the
OAuth flow is the same shape ZIP2 already proved out.

No live Google OAuth app credentials exist in this environment (see
docs/omi-integration.md for the identical situation with Lyzr/Omi) — every
function here is real, correct code against Google's documented OAuth2 and
Calendar v3 API contracts, contract-tested with the HTTP boundary mocked.
Live verification is BLOCKED pending real GOOGLE_OAUTH_CLIENT_ID /
GOOGLE_OAUTH_CLIENT_SECRET, exactly like Lyzr/Omi in Phase 1/2.
"""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_CALENDAR_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

# Write scope (not ZIP2's calendar.readonly) — see module docstring.
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
OAUTH_SCOPES = [GOOGLE_CALENDAR_SCOPE]

GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "")

_HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class GoogleCalendarNotConfigured(RuntimeError):
    """Raised when GOOGLE_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI are unset —
    these are Google Cloud Console app-registration values that cannot be
    inferred, same class of blocker as LYZR_PROVIDER_ID in Phase 1/2."""


class GoogleCalendarError(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI)


def _require_configured() -> None:
    if not is_configured():
        raise GoogleCalendarNotConfigured(
            "Google Calendar requires GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, "
            "and GOOGLE_OAUTH_REDIRECT_URI (from a Google Cloud Console OAuth app) — "
            "these cannot be inferred and must be set in the environment."
        )


def generate_state_token() -> str:
    """CSRF-protection state param for the OAuth redirect — cryptographically
    random, single-use (caller is responsible for storing/validating it,
    e.g. in a short-lived server-side session record)."""
    return secrets.token_urlsafe(32)


def build_authorization_url(state: str) -> str:
    _require_configured()
    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(OAUTH_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(code: str) -> dict:
    _require_configured()
    body = {
        "code": code,
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if resp.status_code >= 400:
        raise GoogleCalendarError(f"Google token exchange failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


async def refresh_access_token(refresh_token: str) -> dict:
    _require_configured()
    body = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if resp.status_code >= 400:
        raise GoogleCalendarError(f"Google token refresh failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


async def fetch_account_email(access_token: str) -> Optional[str]:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"})
    if resp.status_code >= 400:
        return None
    return (resp.json() or {}).get("email")


def token_expires_at(expires_in_seconds: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)


async def create_calendar_event(
    access_token: str,
    *,
    summary: str,
    description: str = "",
    start: datetime,
    end: datetime,
    calendar_id: str = "primary",
) -> dict:
    """Create a Google Calendar event. Raises GoogleCalendarError on any
    non-2xx response rather than pretending success."""
    body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    url = GOOGLE_CALENDAR_EVENTS_URL.format(calendar_id=calendar_id)
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        resp = await client.post(url, json=body, headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"})
    if resp.status_code >= 400:
        raise GoogleCalendarError(f"Google Calendar event creation failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json()
