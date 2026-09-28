"""Per-device authentication helpers for satellite devices.

Centralises the bcrypt-hashed device_secret flow used by:

  * ``/ws/satellite/{device_id}/audio``        — WebSocket streaming
  * ``/api/satellites/{device_id}/heartbeat``  — HTTP heartbeat
  * ``/api/satellites/{device_id}/upload-audio``
  * ``/api/satellites/{device_id}/transcript``
  * ``/api/satellites/{device_id}/start-recording``
  * ``/api/satellites/{device_id}/stop-recording``

Why this lives outside ``auth/`` proper: the rest of ``auth/`` is user-
oriented (JWT, password reset, etc.). Devices are a distinct principal
class — they don't have an org-membership row, they don't browse the UI,
they cannot rotate their own credential. Keeping the surface area
separate prevents accidental coupling (e.g. someone adding a device
shortcut to ``get_current_user``).

Hash algorithm: bcrypt via passlib's CryptContext — same as user
passwords in ``auth/utils.py``. Picking the same algorithm means we get
one rotation story instead of two; if bcrypt is ever deprecated, the
``deprecated="auto"`` flag in the shared CryptContext will mark both
user passwords AND device secrets for rehash on next use.

Rate limiting: in-memory leaky-bucket per (process, device_id). 5
failures inside any 10-minute window triggers a 30-minute lockout.
Tradeoffs documented inline near the implementation. A Redis-backed
version is the obvious follow-up once we have more than one backend
replica streaming satellite traffic — until then the simpler structure
wins.

Plaintext secrets:
  * MUST NEVER be logged
  * MUST NEVER appear in error messages, exception text, or DB columns
    other than the (hashed) ``satellite_devices.device_secret``
  * Are returned to the device EXACTLY ONCE — at pairing-code redemption
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

from fastapi import Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from auth.utils import pwd_context
from database.models import SatelliteDevice

logger = logging.getLogger(__name__)

# Length of the plaintext device secret. 32 bytes of urlsafe-base64 ⇒
# ~43 chars. Sized for "long enough to brute-force is hopeless" with no
# attempt at human readability — devices store this in firmware, not
# users.
_SECRET_BYTES = 32


# ---------------------------------------------------------------------------
# Generation + hashing
# ---------------------------------------------------------------------------


def generate_device_secret() -> str:
    """Return a fresh plaintext device secret. Cryptographically random."""
    return secrets.token_urlsafe(_SECRET_BYTES)


def hash_device_secret(plaintext: str) -> str:
    """Bcrypt-hash a plaintext device secret for storage."""
    return pwd_context.hash(plaintext)


def verify_device_secret(plaintext: str, stored_hash: Optional[str]) -> bool:
    """Verify a plaintext secret against the stored hash.

    Returns False (never raises) on any verification failure, including:
      * ``stored_hash`` is None (legacy device — must re-pair)
      * ``stored_hash`` is malformed
      * ``plaintext`` is empty
      * bcrypt verification returns False

    Constant-time comparison comes from passlib. Do NOT add an early-out
    on empty stored_hash before calling verify — the early-out is fine
    here because the hash is server-side and not attacker-controllable,
    but keep the order (stored_hash check first) so we never feed
    ``None`` into passlib.
    """
    if not stored_hash or not plaintext:
        return False
    try:
        return pwd_context.verify(plaintext, stored_hash)
    except (ValueError, TypeError):
        # Malformed hash on disk. Don't leak the error — just fail closed.
        return False


# ---------------------------------------------------------------------------
# Rate limiting — in-memory leaky bucket
# ---------------------------------------------------------------------------
#
# Why in-memory and not Redis: at v0.7.0 there is exactly one backend
# process. Multi-replica satellite streaming isn't a near-term goal — see
# CLAUDE.md "multi-room native, single-backend Phase 1 primary". The
# in-memory limiter is sufficient today and the API surface is identical
# to a future Redis adapter (one async-friendly object swap behind
# ``_LIMITER``).
#
# Window semantics:
#   * 5 failed auth attempts inside a rolling 10-minute window ⇒ lock the
#     device_id for 30 minutes from the 5th failure
#   * Successful auth resets the counter (so an admin who just reseated
#     the device doesn't have to wait through a lockout if they typo
#     once)
#   * Locked devices receive 1008/policy-violation on WS and 401 on HTTP;
#     the response body NEVER mentions which (lockout vs invalid secret)
#     state we're in — that's part of the brute-force defence

_FAIL_WINDOW_SECONDS = 10 * 60   # 10 minutes
_FAIL_THRESHOLD = 5
_LOCKOUT_SECONDS = 30 * 60       # 30 minutes


@dataclass
class _BucketState:
    failures: list = field(default_factory=list)  # timestamps (floats)
    locked_until: float = 0.0                     # epoch seconds; 0 = not locked


class _AuthRateLimiter:
    """Per-device failure counter + lockout. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, _BucketState] = {}

    def is_locked(self, device_id: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = self._state.get(device_id)
            if not bucket:
                return False
            if bucket.locked_until and bucket.locked_until > now:
                return True
            # Lock expired — clear it but keep failure history pruned.
            if bucket.locked_until and bucket.locked_until <= now:
                bucket.locked_until = 0.0
                bucket.failures = []
        return False

    def record_failure(self, device_id: str) -> bool:
        """Record a failed auth attempt for *device_id*.

        Returns True if this failure pushed the bucket over the lockout
        threshold (so callers can log the event once at the boundary).
        """
        now = time.time()
        with self._lock:
            bucket = self._state.setdefault(device_id, _BucketState())
            cutoff = now - _FAIL_WINDOW_SECONDS
            bucket.failures = [t for t in bucket.failures if t >= cutoff]
            bucket.failures.append(now)
            if len(bucket.failures) >= _FAIL_THRESHOLD and not bucket.locked_until:
                bucket.locked_until = now + _LOCKOUT_SECONDS
                return True
        return False

    def record_success(self, device_id: str) -> None:
        """Clear the failure bucket on successful auth."""
        with self._lock:
            self._state.pop(device_id, None)

    # Testing hook — never called in production code paths.
    def _reset_all(self) -> None:
        with self._lock:
            self._state.clear()


_LIMITER = _AuthRateLimiter()


def reset_rate_limiter_for_tests() -> None:
    """Tests call this to reset the in-memory limiter between cases.

    Production code MUST NOT call this — clearing the limiter would
    erase active lockouts.
    """
    _LIMITER._reset_all()


# ---------------------------------------------------------------------------
# Public verification entry points
# ---------------------------------------------------------------------------


@dataclass
class DeviceAuthResult:
    device: SatelliteDevice
    org_id: int


class DeviceAuthError(HTTPException):
    """401 with a generic body that never leaks lockout state or
    distinguishes "no secret on file" from "wrong secret"."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Device authentication failed",
        )


def _looks_like_jwt(token: str) -> bool:
    """Heuristic: JWTs have exactly two dots separating three base64url
    segments. Device secrets are single urlsafe-base64 strings — no dots.

    This lets us share ``Authorization: Bearer`` between user JWTs and
    device secrets without disambiguating headers, while keeping the
    user-flow and device-flow paths separate. The heuristic is one-way:
    a token that "looks like a JWT" is left alone for the user-auth
    layer; a token that doesn't look like a JWT is treated as a device
    secret candidate.

    False positives (a device secret that happens to contain dots): not
    possible — ``secrets.token_urlsafe`` only emits ``[A-Za-z0-9_-]``.
    False negatives (a JWT mistaken for a device secret): also not
    possible — every JWT has 3 dot-separated segments.
    """
    # A JWT is "header.payload.signature" — exactly 2 dots between non-
    # empty segments. We accept the loose definition because the call
    # site only needs to know "should I pass this to the device flow?".
    if not token:
        return False
    parts = token.split(".")
    return len(parts) == 3 and all(p for p in parts)


def _extract_secret_from_request(
    *,
    authorization: Optional[str],
    x_device_secret: Optional[str],
) -> Optional[str]:
    """Pull the plaintext device secret out of an HTTP request.

    Order of preference:
      1. ``X-Device-Secret`` header — unambiguous, always treated as a
         device-flow credential.
      2. ``Authorization: Bearer <token>`` — only treated as a device
         secret if the token does NOT look like a JWT. This lets admin
         user sessions (Bearer <JWT>) coexist with device sessions
         (Bearer <device_secret>) on the dual-auth endpoints without
         clobbering each other.

    Returns the candidate secret or ``None`` if neither header is set
    in a way that yields a device-secret-shaped value.
    """
    if x_device_secret:
        candidate = x_device_secret.strip()
        if candidate:
            return candidate
    if authorization:
        # Be tolerant of casing — RFC 7235 lets the scheme be any case.
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            candidate = parts[1].strip()
            if candidate and not _looks_like_jwt(candidate):
                return candidate
    return None


def authenticate_device(
    *,
    db: Session,
    device_id: str,
    plaintext_secret: Optional[str],
) -> DeviceAuthResult:
    """Look up the device by ``device_id`` and verify the plaintext secret.

    Raises ``DeviceAuthError`` on:
      * device row not found
      * device row has no secret on file (legacy / orphan)
      * secret mismatch
      * device is currently locked-out by the rate limiter

    On success, resets the rate limiter for this device and returns the
    SatelliteDevice + org_id.

    Generic 401 on every failure path — callers MUST NOT add detail that
    would let an attacker distinguish "unknown device" from "wrong
    secret" from "locked out".
    """
    if _LIMITER.is_locked(device_id):
        # Don't even bother hitting the DB on a locked device. We also
        # don't increment the counter — the lockout already gates access.
        raise DeviceAuthError()

    if not plaintext_secret:
        # Missing-credential failures count toward the lockout too, so
        # a bot scanning for unauthenticated WS endpoints gets locked
        # out just like a brute-forcer.
        _record_failure_and_log(device_id, reason="missing_secret")
        raise DeviceAuthError()

    device = db.query(SatelliteDevice).filter(
        SatelliteDevice.device_id == device_id
    ).first()

    if device is None:
        _record_failure_and_log(device_id, reason="unknown_device")
        raise DeviceAuthError()

    if not verify_device_secret(plaintext_secret, device.device_secret):
        _record_failure_and_log(device_id, reason="bad_secret")
        raise DeviceAuthError()

    _LIMITER.record_success(device_id)
    return DeviceAuthResult(device=device, org_id=device.organization_id or 0)


def _record_failure_and_log(device_id: str, *, reason: str) -> None:
    """Helper: bump the limiter, log if this attempt triggered a lockout."""
    locked_now = _LIMITER.record_failure(device_id)
    if locked_now:
        # Lockout is a noisy event — surface it at WARNING. Never log
        # the plaintext secret or any header values.
        logger.warning(
            "Satellite device auth: locked out device_id=%s (%s failures in "
            "%ds, lockout %ds)",
            device_id,
            _FAIL_THRESHOLD,
            _FAIL_WINDOW_SECONDS,
            _LOCKOUT_SECONDS,
        )
    else:
        logger.info(
            "Satellite device auth failure: device_id=%s reason=%s",
            device_id,
            reason,
        )


# ---------------------------------------------------------------------------
# FastAPI dependency: optional device auth on HTTP endpoints
# ---------------------------------------------------------------------------


def device_auth_from_http(
    request: Request,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    x_device_secret: Optional[str] = Header(default=None, alias="X-Device-Secret"),
) -> Optional[str]:
    """Pull the plaintext device secret out of an incoming HTTP request.

    Returns the secret string if present (in either Authorization Bearer
    or X-Device-Secret), or None if absent. The caller decides whether
    absence is a hard failure (state-mutating satellite endpoints) or
    means "fall through to user auth" (admin UI flows).

    This is intentionally a passive extractor — it does NOT call the DB
    or verify the secret. That keeps the dependency cheap and lets the
    endpoint pick its own dual-auth policy.
    """
    return _extract_secret_from_request(
        authorization=authorization,
        x_device_secret=x_device_secret,
    )
