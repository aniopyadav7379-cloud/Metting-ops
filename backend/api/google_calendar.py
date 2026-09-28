"""Google Calendar API (Phase 3F).

Action Item -> user confirmation -> Google Calendar -> Calendar event.

Live verification is BLOCKED without real GOOGLE_OAUTH_CLIENT_ID/SECRET —
see services/integrations/google_calendar.py's module docstring. Every
endpoint here is real, working code against real request/response
contracts; only the actual OAuth exchange with Google's servers has not
been (and cannot be) exercised from this environment.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from auth.dependencies import get_current_organization, get_current_user
from auth.models import User
from auth.organization import ActiveOrganization
from database.database import get_db
from database.models import ActionItem, GoogleCalendarIntegration
from services.integrations.google_calendar import (
    GoogleCalendarError,
    GoogleCalendarNotConfigured,
    build_authorization_url,
    create_calendar_event,
    exchange_code_for_tokens,
    fetch_account_email,
    generate_state_token,
    is_configured,
    refresh_access_token,
    token_expires_at,
)
from services.providers.crypto import decrypt_api_key, encrypt_api_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations/google-calendar", tags=["Google Calendar"])

# In-memory state-token store (single-process; a multi-worker deployment
# would move this to Redis/DB the same way this codebase already does for
# other short-lived, single-use tokens — noted as a scaling TODO, not a
# correctness issue for the OAuth flow itself).
_pending_states: dict[str, dict] = {}
_STATE_TTL = timedelta(minutes=10)


class ConnectionStatus(BaseModel):
    configured: bool
    connected: bool
    google_email: Optional[str] = None
    status: str = "disconnected"
    connected_at: Optional[str] = None
    last_sync_error: Optional[str] = None


class AuthorizeResponse(BaseModel):
    authorization_url: str


def _row(db: Session, org_id: int, user_id: int) -> Optional[GoogleCalendarIntegration]:
    return (
        db.query(GoogleCalendarIntegration)
        .filter(GoogleCalendarIntegration.organization_id == org_id, GoogleCalendarIntegration.user_id == user_id)
        .first()
    )


@router.get("/status", response_model=ConnectionStatus)
async def get_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    row = _row(db, active_org.organization.id, current_user.id)
    if row is None:
        return ConnectionStatus(configured=is_configured(), connected=False)
    return ConnectionStatus(
        configured=is_configured(),
        connected=row.status == "connected",
        google_email=row.google_email,
        status=row.status,
        connected_at=row.connected_at.isoformat() if row.connected_at else None,
        last_sync_error=row.last_sync_error,
    )


@router.get("/authorize", response_model=AuthorizeResponse)
async def authorize(
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    if not is_configured():
        raise HTTPException(status_code=503, detail="Google Calendar is not configured on this server (missing OAuth app credentials)")
    state = generate_state_token()
    _pending_states[state] = {
        "organization_id": active_org.organization.id,
        "user_id": current_user.id,
        "expires_at": datetime.now(timezone.utc) + _STATE_TTL,
    }
    return AuthorizeResponse(authorization_url=build_authorization_url(state))


@router.get("/callback")
async def oauth_callback(
    code: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    """Google redirects the user's browser here after consent. Not behind
    get_current_user — the state token (single-use, TTL'd, minted only by
    /authorize for a specific already-authenticated user) is what proves
    which user/org this callback belongs to, the same CSRF-protection
    pattern ZIP2's oauth-state-cookie.ts uses, adapted to a server-side
    store instead of a cookie."""
    pending = _pending_states.pop(state, None)
    if pending is None:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    if pending["expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="OAuth state expired — please retry connecting")

    try:
        tokens = await exchange_code_for_tokens(code)
    except GoogleCalendarNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GoogleCalendarError as exc:
        raise HTTPException(status_code=502, detail=f"Google rejected the authorization: {exc}") from exc

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in", 3600)
    if not access_token:
        raise HTTPException(status_code=502, detail="Google did not return an access token")

    email = await fetch_account_email(access_token)

    row = _row(db, pending["organization_id"], pending["user_id"])
    if row is None:
        row = GoogleCalendarIntegration(organization_id=pending["organization_id"], user_id=pending["user_id"])
        db.add(row)
    row.google_email = email
    row.access_token_encrypted = encrypt_api_key(access_token)
    if refresh_token:
        # Google only returns a refresh_token on the FIRST consent (or when
        # prompt=consent forces re-issuance, which authorize() always
        # requests) — never overwrite a previously-stored one with nothing.
        row.refresh_token_encrypted = encrypt_api_key(refresh_token)
    row.token_expires_at = token_expires_at(expires_in)
    row.scopes = tokens.get("scope", "")
    row.status = "connected"
    row.last_sync_error = None
    row.connected_at = datetime.now(timezone.utc)
    db.commit()

    return {"ok": True, "google_email": email}


@router.post("/disconnect")
async def disconnect(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    row = _row(db, active_org.organization.id, current_user.id)
    if row is None:
        return {"ok": True, "was_connected": False}
    was_connected = row.status == "connected"
    # Tokens are cleared, not just status-flagged, so a disconnected
    # integration can never be reused to call the Calendar API again.
    row.access_token_encrypted = None
    row.refresh_token_encrypted = None
    row.token_expires_at = None
    row.status = "disconnected"
    db.commit()
    return {"ok": True, "was_connected": was_connected}


async def _get_valid_access_token(db: Session, row: GoogleCalendarIntegration) -> str:
    """Returns a usable access token, refreshing first if expired.
    Raises GoogleCalendarError if there is nothing usable (never connected,
    disconnected, or refresh itself failed)."""
    if row.status != "connected" or not row.access_token_encrypted:
        raise GoogleCalendarError("Google Calendar is not connected for this user")

    now = datetime.now(timezone.utc)
    expires_at = row.token_expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at and expires_at > now + timedelta(seconds=60):
        return decrypt_api_key(row.access_token_encrypted)

    if not row.refresh_token_encrypted:
        row.status = "expired"
        row.last_sync_error = "Access token expired and no refresh token is stored"
        db.commit()
        raise GoogleCalendarError(row.last_sync_error)

    try:
        tokens = await refresh_access_token(decrypt_api_key(row.refresh_token_encrypted))
    except (GoogleCalendarNotConfigured, GoogleCalendarError) as exc:
        row.status = "expired"
        row.last_sync_error = str(exc)
        db.commit()
        raise

    new_access = tokens.get("access_token")
    if not new_access:
        row.status = "expired"
        row.last_sync_error = "Token refresh did not return a new access token"
        db.commit()
        raise GoogleCalendarError(row.last_sync_error)

    row.access_token_encrypted = encrypt_api_key(new_access)
    row.token_expires_at = token_expires_at(tokens.get("expires_in", 3600))
    row.last_sync_error = None
    db.commit()
    return new_access


class CreateEventRequest(BaseModel):
    start: Optional[datetime] = None
    duration_minutes: int = 30


class CreateEventResponse(BaseModel):
    ok: bool
    google_event_id: Optional[str] = None
    error: Optional[str] = None


@router.post("/action-items/{action_item_id}/create-event", response_model=CreateEventResponse)
async def create_event_from_action_item(
    action_item_id: int,
    request: CreateEventRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
):
    """The explicit 'user confirmation' step: this endpoint is the
    confirmation — nothing creates a calendar event automatically when an
    action item is extracted, only when a user calls this route."""
    org_id = active_org.organization.id
    item = (
        db.query(ActionItem)
        .filter(ActionItem.id == action_item_id, ActionItem.organization_id == org_id)
        .first()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Action item not found")

    row = _row(db, org_id, current_user.id)
    if row is None or row.status != "connected":
        raise HTTPException(status_code=400, detail="Connect Google Calendar first (GET /authorize)")

    try:
        access_token = await _get_valid_access_token(db, row)
    except GoogleCalendarError as exc:
        item.google_calendar_link_state = "failed"
        item.google_calendar_sync_error = str(exc)
        db.commit()
        return CreateEventResponse(ok=False, error=str(exc))

    start = request.start or (item.due_date or (datetime.now(timezone.utc) + timedelta(days=1)))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    end = start + timedelta(minutes=max(5, request.duration_minutes))

    try:
        event = await create_calendar_event(
            access_token,
            summary=item.text[:200] if item.text else "Action item",
            description=f"Owner: {item.owner or 'unassigned'}\nCreated from Meeting-Ops action item #{item.id}.",
            start=start,
            end=end,
        )
    except GoogleCalendarError as exc:
        item.google_calendar_link_state = "failed"
        item.google_calendar_sync_error = str(exc)
        db.commit()
        return CreateEventResponse(ok=False, error=str(exc))

    item.google_calendar_event_id = event.get("id")
    item.google_calendar_link_state = "linked"
    item.google_calendar_synced_at = datetime.now(timezone.utc)
    item.google_calendar_sync_error = None
    db.commit()
    return CreateEventResponse(ok=True, google_event_id=event.get("id"))
