"""Internal-service auth for backend-to-backend loopback calls.

Some backend components (room_recorder is the first) POST into the
public HTTP API over the loopback interface so they can reuse the full
chunk → STT → diarization → broadcast pipeline. Those callers are NOT
end users, they cannot present a JWT, and giving them a "real" user
account is the wrong abstraction (audit trails would lie, quotas would
attribute usage to a fictional human, etc.).

This module wires a shared-secret token that the chunks endpoints accept
in addition to normal user auth. The token:

  * Lives only in the backend container's env (``INTERNAL_SERVICE_TOKEN``).
  * Is compared in constant time with ``secrets.compare_digest`` — it's a
    static shared secret, so the timing-attack class is real and trivial
    to close.
  * Is bound to a small, explicit allow-list of endpoints (chunks +
    chunks-text). Every other route still requires normal user auth.
  * Is treated as fail-closed: if the env var is unset/empty, ANY
    request that tries to use the internal token is rejected; user auth
    is still honoured.

The caller surface is a small sentinel class ``InternalServiceCaller``
that endpoints can ``isinstance``-check to know they're in the trusted
loopback path. The sentinel intentionally has no DB identity (no user,
no org) so endpoints can't accidentally treat it like a real account —
they must explicitly resolve org context from the request payload (e.g.
look up the session row and read its ``organization_id``).
"""
from __future__ import annotations

import logging
import os
import secrets
from typing import Optional

from fastapi import Depends, Header, HTTPException, status

from auth.dependencies import get_current_user_optional
from auth.models import User

logger = logging.getLogger(__name__)


INTERNAL_TOKEN_HEADER = "X-Internal-Service-Token"


class InternalServiceCaller:
    """Sentinel returned by ``get_internal_or_user`` for loopback callers.

    Has no user/org identity by design — endpoints that accept this
    caller MUST resolve org context from the request payload (e.g. the
    session row whose ID is in the URL). Treating it as a stand-in for
    a User would silently bypass org scoping for the rest of the
    endpoint body.
    """

    __slots__ = ("provenance",)

    def __init__(self, provenance: Optional[str] = None) -> None:
        # Optional free-text tag the caller can set (e.g. "room-recorder").
        # Captured only for log lines; never used for authz decisions.
        self.provenance = provenance


def _configured_token() -> str:
    return os.environ.get("INTERNAL_SERVICE_TOKEN", "") or ""


def _internal_token_valid(presented: Optional[str]) -> bool:
    """Constant-time compare. False if either side is empty.

    Empty env var ⇒ feature is OFF ⇒ no internal-token request can
    succeed (fail-closed). We log loudly the first time someone tries
    so misconfiguration shows up in ops dashboards.
    """
    configured = _configured_token()
    if not configured:
        # Use module-level flag to keep noise down: log once per process.
        if not getattr(_internal_token_valid, "_warned", False):
            logger.error(
                "INTERNAL_SERVICE_TOKEN is unset/empty; all internal-service "
                "token attempts will be rejected. Set the env var to enable "
                "room_recorder → backend loopback POSTs."
            )
            _internal_token_valid._warned = True  # type: ignore[attr-defined]
        return False
    if not presented:
        return False
    # `compare_digest` requires equal-length bytes or it short-circuits;
    # encode both sides explicitly so a sneaky non-string presented value
    # can't change the timing profile.
    try:
        return secrets.compare_digest(
            presented.encode("utf-8"), configured.encode("utf-8")
        )
    except (AttributeError, TypeError):
        return False


async def get_internal_or_user(
    x_internal_token: Optional[str] = Header(default=None, alias=INTERNAL_TOKEN_HEADER),
    x_room_recorder_provenance: Optional[str] = Header(default=None),
    user: Optional[User] = Depends(get_current_user_optional),
):
    """Dual-auth dependency: accept a valid internal token OR a real user.

    Order of precedence:
      1. Valid ``X-Internal-Service-Token`` → ``InternalServiceCaller``
      2. Valid user auth (JWT / SSO / API key) → ``User``
      3. Neither → 401

    NOTE: An *invalid* internal token does NOT short-circuit. If the
    caller also presents valid user auth we fall through to the user
    branch. This mirrors how real proxies behave when a request carries
    a malformed extra header alongside legitimate credentials, and it
    means a leaked-but-stale token can't accidentally be used to
    "trade down" a real user's session into an internal-flagged one
    (we never set the internal flag without a *valid* token).
    """
    if x_internal_token:
        if _internal_token_valid(x_internal_token):
            return InternalServiceCaller(provenance=x_room_recorder_provenance)
        # Invalid token + no user → log as a likely attack, then 401.
        # Invalid token + valid user → fall through silently (the user is
        # legitimately authed, the bad token is just noise).
        if not user:
            logger.warning(
                "Rejected request with invalid %s header (no user auth fallback).",
                INTERNAL_TOKEN_HEADER,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid internal service token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    if user:
        return user
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


__all__ = [
    "INTERNAL_TOKEN_HEADER",
    "InternalServiceCaller",
    "get_internal_or_user",
]
