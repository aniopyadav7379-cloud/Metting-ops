"""Shared WebSocket handshake authentication for the meeting sockets (v3.29.3).

Starlette WS handlers can't use ``Depends(get_db)`` / ``Depends(get_current_user)``,
so auth has to be done by hand at the handshake. This mirrors the already-proven
pattern in ``api/websocket_remote_audio.py`` (``_authenticate_websocket``) and
``api/streaming.py`` (``_resolve_ws_user``): read a JWT from the ``?token=`` query
param (the browser WebSocket API can't set headers), validate it with the same
``decode_token`` the REST stack uses, and materialise the caller's
org-membership ids onto ``user._org_ids`` so a session-bound socket can be
org-scoped after the DB session closes.

``WS_REQUIRE_AUTH`` (default true) is a kill-switch: set it to false to fall back
to the pre-v3.29.3 open behaviour if a client somehow can't present a token
(instant rollback without a redeploy). Enforcement is belt-and-suspenders on the
cloud node (oauth2-proxy already fronts these), and the real win on a self-hosted
node with no proxy in front.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Starlette/RFC6455 close code for an auth/policy failure.
WS_CLOSE_POLICY_VIOLATION = 1008


def ws_auth_required() -> bool:
    """True unless WS_REQUIRE_AUTH is explicitly disabled (kill-switch)."""
    return os.getenv("WS_REQUIRE_AUTH", "true").strip().lower() in ("1", "true", "yes", "on")


async def authenticate_ws(websocket: WebSocket):
    """Validate the ``?token=`` JWT on a WS handshake.

    Returns the active ``User`` (detached, with ``_org_ids`` materialised) on
    success, or ``None`` on any failure. Never raises. Does NOT accept/close the
    socket — the caller decides what to do based on ``ws_auth_required()``.
    """
    token = websocket.query_params.get("token")
    if not token:
        # Native-OIDC nodes (prod, no oauth2-proxy) sign users in with the
        # HttpOnly ``mo_uc_session`` cookie that auth/oidc_sso.py sets — itself a
        # standard access JWT (sub=user id). The browser can't set WS headers and
        # native-OIDC keeps no localStorage token, so fall back to that cookie
        # here. Same source the REST stack reads in auth/dependencies.py, decoded
        # by the same ``decode_token`` below — so no new trust path is added.
        import os

        token = websocket.cookies.get(
            os.getenv("UC_SSO_COOKIE_NAME", "mo_uc_session")
        )
    if not token:
        return None
    token = token.strip()
    if not token:
        return None

    try:
        from auth.utils import decode_token
        from auth.service import AuthService
        from auth.models import UserOrganization
        from database.database import SessionLocal

        payload = decode_token(token)
        if not payload or payload.get("type") != "access":
            return None
        user_id_str = payload.get("sub")
        if not user_id_str:
            return None
        user_id = int(user_id_str)

        db = SessionLocal()
        try:
            user = AuthService.get_user_by_id(db, user_id)
            if user and user.is_active:
                org_ids = [
                    row[0]
                    for row in db.query(UserOrganization.organization_id)
                    .filter(UserOrganization.user_id == user.id)
                    .all()
                ]
                db.expunge(user)
                user._org_ids = org_ids
                return user
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("WS JWT auth failed: %s", e)
    return None


def _session_org_id(session_id: str) -> Optional[int]:
    """Resolve a RecordingSession's organization_id by session_id (uuid) or pk.

    Returns None if the session can't be found (don't leak existence — the
    caller treats None as "nothing to scope against", and a real cross-org id
    mismatch is what gets rejected). Best-effort; never raises."""
    try:
        from database.database import SessionLocal
        from database.models import RecordingSession

        db = SessionLocal()
        try:
            row = (
                db.query(RecordingSession.organization_id)
                .filter(RecordingSession.session_id == session_id)
                .first()
            )
            if row is None:
                try:
                    row = (
                        db.query(RecordingSession.organization_id)
                        .filter(RecordingSession.id == int(session_id))
                        .first()
                    )
                except (TypeError, ValueError):
                    row = None
            return int(row[0]) if row and row[0] is not None else None
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("WS session org resolve failed for %s: %s", session_id, e)
        return None


def resolve_session_org(session_id: str):
    """Load the Organization that owns ``session_id`` (detached) or None.

    The session's owning org IS the active workspace for a session-bound socket:
    the meeting belongs to it and the per-workspace billing gate must read its
    plan (gate on the active workspace's entitlement, not the user's global
    tier). Best-effort; never raises. Returns None when unresolvable, so callers
    fall back to the user-tier gate (``gate_feature_for_caller`` active_org=None).
    """
    org_id = _session_org_id(session_id)
    if org_id is None:
        return None
    try:
        from auth.organization import load_organization
        from database.database import SessionLocal

        db = SessionLocal()
        try:
            org = load_organization(db, org_id)
            if org is not None:
                db.expunge(org)
            return org
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("WS active-org resolve failed for %s: %s", session_id, e)
        return None


def ws_user_can_access_session(user, session_id: str) -> bool:
    """True if ``user`` may attach to ``session_id``'s socket.

    A session that resolves to an org the user is NOT a member of is rejected.
    An unresolvable session (None) is allowed through — the handler then deals
    with it and we avoid leaking which session ids exist. Superusers pass."""
    if getattr(user, "is_superuser", False):
        return True
    org_id = _session_org_id(session_id)
    if org_id is None:
        return True
    return org_id in getattr(user, "_org_ids", [])


async def enforce_ws_auth(websocket: WebSocket, *, session_id: Optional[str] = None):
    """Authenticate (and optionally org-scope) a WS handshake.

    Returns ``(ok, user)``. When ``ws_auth_required()`` is true and auth fails,
    closes the socket with 1008 and returns ``(False, None)`` — the caller must
    ``return`` immediately. When the kill-switch is off, returns ``(True, user)``
    (``user`` may be None) so the socket stays open as before.
    """
    user = await authenticate_ws(websocket)
    required = ws_auth_required()

    if user is None:
        if required:
            await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
            return False, None
        return True, None  # kill-switch off → legacy open behaviour

    if session_id is not None and not ws_user_can_access_session(user, session_id):
        # Authenticated but not entitled to THIS session → always reject,
        # regardless of the kill-switch (this is cross-tenant protection).
        await websocket.close(code=WS_CLOSE_POLICY_VIOLATION)
        return False, None

    return True, user
