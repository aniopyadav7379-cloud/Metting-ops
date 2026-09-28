"""Opaque, one-time-visible secrets for per-meeting invitations.

Only SHA-256 digests are persisted.  The raw secret is returned to the caller
that creates or rotates it and must not be logged, serialized by list
endpoints, or written to the database.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit


INVITATION_TOKEN_VERSION = 2
LEGACY_INVITATION_TOKEN_VERSION = 1
INVITATION_ISSUANCE_UNAVAILABLE = (
    "Invitation link issuance is temporarily unavailable"
)

# Legacy UUID links migrated by revision 053 remain usable only during this
# transition.  An environment value may shorten the window, but cannot extend
# it past this code-level ceiling.
LEGACY_INVITATION_MAX_CUTOFF = datetime(
    2026, 10, 31, 0, 0, 0, tzinfo=timezone.utc
)


def generate_invitation_secret() -> str:
    """Return a URL-safe secret with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def invitation_v2_issuance_enabled() -> bool:
    """Return whether this process may mint a new v2 bearer secret.

    The default is deliberately off. Operators enable this only after every
    old API process has drained, so no old process can mistake a compatibility
    UUID for the real bearer value during a mixed-version rollout.
    """
    return (
        os.getenv("MEETING_INVITE_V2_ISSUANCE_ENABLED", "")
        .strip()
        .casefold()
        in {"1", "true", "yes", "on"}
    )


def hash_invitation_secret(secret: str) -> str:
    """Return the stable lookup digest for an invitation secret."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def invitation_resend_minimum_interval_seconds() -> int:
    """Return the bounded resend interval used by every invitation sender.

    Keeping this at the token-service boundary prevents the explicit share
    control and the email-a-copy workflow from drifting into different retry
    policies. Bad operational configuration fails to the safe default.
    """
    try:
        configured = int(
            os.getenv("MEETING_INVITE_RESEND_MIN_INTERVAL_SECONDS", "60")
        )
    except ValueError:
        configured = 60
    return max(30, min(configured, 3600))


def legacy_invitation_is_allowed(
    token_version: int | None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether a migrated v1 UUID invitation is still in its bounded window."""
    if token_version != LEGACY_INVITATION_TOKEN_VERSION:
        return True

    cutoff = LEGACY_INVITATION_MAX_CUTOFF
    configured = os.getenv("MEETING_INVITE_LEGACY_TOKEN_CUTOFF", "").strip()
    if configured:
        try:
            parsed = datetime.fromisoformat(configured.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            cutoff = min(cutoff, parsed.astimezone(timezone.utc))
        except ValueError:
            # Fail closed for malformed operational configuration.
            return False

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc) < cutoff


def _is_development_environment() -> bool:
    environment = (
        os.getenv("ENVIRONMENT", "").strip()
        or os.getenv("APP_ENV", "").strip()
    ).casefold()
    return environment in {"dev", "development", "local", "test"}


def _is_loopback_hostname(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def invitation_delivery_url_is_allowed(url: str) -> bool:
    """Whether an invitation URL is safe to place in outbound email."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    if (
        not parsed.hostname
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
    ):
        return False
    if parsed.scheme.casefold() == "https":
        return port is None or 1 <= port <= 65535
    return (
        parsed.scheme.casefold() == "http"
        and _is_development_environment()
        and _is_loopback_hostname(parsed.hostname)
        and (port is None or 1 <= port <= 65535)
    )


def _configured_public_base() -> str:
    configured_base = (
        os.getenv("MEETING_OPS_PUBLIC_URL", "").strip()
        or os.getenv("APP_PUBLIC_URL", "").strip()
        or ""
    ).rstrip("/")
    if not configured_base:
        return ""
    try:
        parsed = urlsplit(configured_base)
    except ValueError:
        return ""
    if parsed.query or parsed.fragment:
        return ""
    return (
        configured_base
        if invitation_delivery_url_is_allowed(configured_base)
        else ""
    )


def build_invitation_url(
    secret: str,
    *,
    require_public: bool = False,
) -> str:
    """Build a link whose secret stays in the browser fragment.

    URL fragments are not sent in HTTP request lines, avoiding disclosure in
    reverse-proxy access logs.  The SPA posts the secret in a JSON body.
    """
    configured_base = _configured_public_base()
    suffix = f"/invite-bootstrap.html#token={quote(secret, safe='')}"
    candidate = f"{configured_base}{suffix}" if configured_base else suffix
    if invitation_delivery_url_is_allowed(candidate):
        return candidate
    if require_public:
        raise ValueError("public_url_not_configured")
    # Manual copy in the authenticated UI may safely resolve a relative URL
    # against its own origin even when outbound email delivery is disabled.
    return suffix


def build_authenticated_session_url(session_id: str | int) -> str:
    """Build a validated, bearer-free URL for an already-authorized user."""
    configured_base = _configured_public_base()
    suffix = f"/sessions/{quote(str(session_id), safe='')}"
    candidate = f"{configured_base}{suffix}" if configured_base else suffix
    if not invitation_delivery_url_is_allowed(candidate):
        raise ValueError("public_url_not_configured")
    return candidate


def smtp_plaintext_is_allowed(hostname: str) -> bool:
    """Permit plaintext SMTP only to loopback in an explicit dev/test env."""
    return _is_development_environment() and _is_loopback_hostname(hostname)
