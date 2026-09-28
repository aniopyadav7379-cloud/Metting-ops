"""Brigade federation JWT verifier (INBOUND).

Accepts short-lived RS256/EdDSA JWTs minted by Unicorn Brigade for
Meeting-Ops and returns the verified claims. This is the inbound half of
federation — the outbound knowledge-graph client lives in
``services.brigade_client``. Mirrors the proven Contact-Ops /
Accounting-Ops verifier (same suite federation contract), adapted to
Meeting-Ops' env-based config + stdlib logging (no pydantic Settings,
no structlog).

Trust contract (SUITE-IDENTITY 0.1):
  * Signature: Brigade JWKS (rotating) fetched from ``BRIGADE_JWKS_URL``,
    cached 24h with a kid-miss force-refresh for key rotation.
  * ``aud`` == ``BRIGADE_EXPECTED_AUDIENCE``   (default "meeting-ops")
  * ``iss`` == ``BRIGADE_TRUSTED_ISSUER``      (default Brigade prod)
  * exp / nbf / iat required; 30s clock-skew leeway.
  * Tenancy claim is ``workspace_id`` (legacy ``org_id`` is not accepted).
    Binding to a Meeting-Ops org is
    done by the caller (api.federation_meetings.org_for_workspace_id) —
    NEVER from a header.

Fail-closed: any parse / fetch / claim error -> invalid, logged without
token content. If ``BRIGADE_JWKS_URL`` is unset the verifier cannot fetch
keys and EVERY token is rejected — that is the dormant / ship-dark state
until the deploy env is wired and Brigade is allow-listed to mint
meeting-ops-audience tokens.

JWT lib: python-jose (``from jose import ...``). NOT PyJWT — the rest of
the backend is python-jose and mixing the two silently breaks decode.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional, cast

import httpx
from jose import jwk, jwt
from jose.exceptions import (
    ExpiredSignatureError,
    JWKError,
    JWTClaimsError,
    JWTError,
)

logger = logging.getLogger(__name__)

ALLOWED_BRIGADE_ALGORITHMS = ["EdDSA", "RS256"]
BRIGADE_JWKS_TTL_SECONDS = 24 * 60 * 60
BRIGADE_CLOCK_SKEW_SECONDS = 30

# Meeting-Ops personal-access-token prefix — never a federation token.
_PAT_PREFIX = "mops_pat_"


def _jwks_url() -> str:
    return os.getenv("BRIGADE_JWKS_URL", "").strip()


def _expected_audience() -> str:
    return os.getenv("BRIGADE_EXPECTED_AUDIENCE", "meeting-ops").strip()


def _trusted_issuer() -> str:
    return os.getenv(
        "BRIGADE_TRUSTED_ISSUER", "https://brigade.unicorncommander.ai"
    ).strip()


@dataclass(frozen=True)
class BrigadeJWTVerificationResult:
    valid: bool
    claims: Optional[dict[str, Any]]
    reason: Optional[str]


class BrigadeJWKSCache:
    """Thread-safe JWKS cache with kid-miss refresh for Brigade key rotation."""

    def __init__(self) -> None:
        self._jwks: Optional[dict[str, Any]] = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def prime(self) -> None:
        self.refresh(force=True)

    def resolve_key(self, kid: str) -> Optional[dict[str, Any]]:
        key = self._find(kid)
        if key is not None:
            return key
        # kid miss -> a rotation may have happened; force one refresh.
        self.refresh(force=True)
        return self._find(kid)

    def refresh(self, *, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if not force and self._jwks is not None and now < self._expires_at:
                return
            url = _jwks_url()
            if not url:
                logger.warning(
                    "brigade_jwks_fetch_failed reason=jwks-url-unconfigured"
                )
                return
            try:
                with httpx.Client(timeout=10) as client:
                    resp = client.get(url)
                    resp.raise_for_status()
                    jwks = cast(dict[str, Any], resp.json())
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "brigade_jwks_fetch_failed reason=jwks-fetch-failed error=%s",
                    exc,
                )
                return
            self._jwks = jwks
            self._expires_at = now + BRIGADE_JWKS_TTL_SECONDS
            logger.info(
                "brigade_jwks_refreshed keys=%d ttl_s=%d",
                len(jwks.get("keys", [])),
                BRIGADE_JWKS_TTL_SECONDS,
            )

    def _find(self, kid: str) -> Optional[dict[str, Any]]:
        now = time.time()
        if self._jwks is None or now >= self._expires_at:
            self.refresh(force=False)
        if self._jwks is None:
            return None
        keys = self._jwks.get("keys", [])
        if not isinstance(keys, list):
            return None
        for key in keys:
            if isinstance(key, dict) and key.get("kid") == kid:
                return cast(dict[str, Any], key)
        return None


_default_cache: Optional[BrigadeJWKSCache] = None
_default_cache_lock = threading.Lock()


def get_brigade_jwks_cache() -> BrigadeJWKSCache:
    global _default_cache
    with _default_cache_lock:
        if _default_cache is None:
            _default_cache = BrigadeJWKSCache()
        return _default_cache


def prime_brigade_jwks_cache() -> None:
    """Best-effort warm of the JWKS cache. Safe to call at startup; a
    no-op (logged) when BRIGADE_JWKS_URL is unset. The cache also
    self-warms lazily on the first token verification, so priming is
    purely a latency optimization."""
    try:
        get_brigade_jwks_cache().prime()
    except Exception as exc:  # noqa: BLE001
        logger.warning("brigade_jwks_prime_failed error=%s", exc)


def extract_scopes(claims: dict[str, Any] | None) -> set[str]:
    """Pull the granted scopes from a verified Brigade claim set.

    Accepts ``scope`` (OAuth space-delimited string) or ``scopes``
    (JSON array). Returns an empty set when neither is present."""
    if not claims:
        return set()
    raw = claims.get("scope")
    if raw is None:
        raw = claims.get("scopes")
    if isinstance(raw, str):
        return {s for s in raw.split() if s}
    if isinstance(raw, Iterable):
        return {str(s) for s in raw if s}
    return set()


def verify_brigade_jwt(token: str) -> Optional[dict[str, Any]]:
    return verify_brigade_jwt_with_reason(token).claims


def verify_brigade_jwt_with_reason(token: str) -> BrigadeJWTVerificationResult:
    return _verify(token, jwks_cache=get_brigade_jwks_cache())


def _verify(
    token: str, *, jwks_cache: BrigadeJWKSCache
) -> BrigadeJWTVerificationResult:
    if not token or token.startswith(_PAT_PREFIX):
        return _reject("personal-access-token")

    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        return _reject("header-unparseable", error=str(exc))

    alg = header.get("alg")
    if alg not in ALLOWED_BRIGADE_ALGORITHMS:
        return _reject("alg-rejected", alg=alg)

    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        return _reject("missing-kid")

    key = jwks_cache.resolve_key(kid)
    if key is None:
        return _reject("kid-not-found", kid=kid)

    try:
        signing_key = jwk.construct(key, algorithm=alg)
    except (JWKError, ValueError, TypeError) as exc:
        return _reject("invalid-jwks-key", kid=kid, error=str(exc))

    expected_aud = _expected_audience()
    trusted_iss = _trusted_issuer()
    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=ALLOWED_BRIGADE_ALGORITHMS,
            audience=expected_aud,
            issuer=trusted_iss,
            options={
                "require_exp": True,
                "require_nbf": True,
                "require_iat": True,
                "require_iss": True,
                "require_aud": True,
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "leeway": BRIGADE_CLOCK_SKEW_SECONDS,
            },
        )
    except ExpiredSignatureError:
        return _reject("expired", kid=kid)
    except JWTClaimsError as exc:
        msg = str(exc).lower()
        if "audience" in msg:
            return _reject("aud-mismatch", kid=kid)
        if "issuer" in msg:
            return _reject("iss-mismatch", kid=kid)
        if "not yet valid" in msg or "before" in msg:
            return _reject("not-yet-valid", kid=kid)
        return _reject("claims-invalid", kid=kid, error=str(exc))
    except JWTError as exc:
        msg = str(exc).lower()
        if "signature" in msg:
            return _reject("signature-invalid", kid=kid)
        return _reject("invalid-token", kid=kid, error=str(exc))

    # Belt-and-suspenders re-check (python-jose already enforced these via
    # options, but the suite verifier asserts them explicitly too).
    if claims.get("aud") != expected_aud:
        return _reject("aud-mismatch", kid=kid)
    if claims.get("iss") != trusted_iss:
        return _reject("iss-mismatch", kid=kid)
    if not claims.get("sub"):
        return _reject("missing-sub", kid=kid)
    if not claims.get("workspace_id"):
        return _reject("missing-workspace-id", kid=kid)

    return BrigadeJWTVerificationResult(valid=True, claims=claims, reason=None)


def _reject(reason: str, **fields: Any) -> BrigadeJWTVerificationResult:
    extra = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.warning("brigade_jwt_rejected reason=%s %s", reason, extra)
    return BrigadeJWTVerificationResult(valid=False, claims=None, reason=reason)
