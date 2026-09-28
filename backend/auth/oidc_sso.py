"""App-level OIDC SSO for Unicorn Commander (uchub Keycloak).

Lets any uchub-authenticated user sign into Meeting-Ops directly, without an
oauth2-proxy in front:

    GET /api/auth/sso/uc/start    -> redirect to Keycloak authorize
    GET /api/auth/sso/uc/callback -> exchange code, auto-provision via
                                     AuthService.get_or_create_sso_user, mint
                                     the app's own JWT, drop it in an HttpOnly
                                     cookie the auth dependency reads, then
                                     redirect into the app.

The public landing page and the email/password consumer accounts are left
untouched — this only adds a direct "Sign in with Unicorn Commander" path that
reuses the existing SSO auto-provisioning (personal org, free tier). The
Unicorn Commander dashboard app-card links straight to /start for zero-click
SSO (the user already holds a uchub session, so Keycloak completes silently).
"""
import asyncio
import logging
import os
import secrets
import urllib.parse
from datetime import timedelta
from typing import Optional

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from jwt import PyJWKClient
from sqlalchemy.orm import Session

from auth.organization import is_global_admin, parse_group_list
from auth.oc_entitlements import sync_oc_entitlement_grant
from auth.service import AuthService
from auth.utils import create_access_token
from database.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth/sso/uc", tags=["sso"])

# Reuse the backend's existing Keycloak config (set in the prod compose).
_KC_BASE = os.getenv("KEYCLOAK_URL", "https://auth.unicorncommander.ai").rstrip("/")
_KC_REALM = os.getenv("KEYCLOAK_REALM", "uchub")
KC_ISSUER = f"{_KC_BASE}/realms/{_KC_REALM}"
KC_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "meeting-ops-prod")
KC_CLIENT_SECRET = os.getenv("KEYCLOAK_CLIENT_SECRET", "")
# JWKS endpoint for verifying the id_token signature (RS256). Keycloak serves
# the realm's public signing keys here; PyJWKClient fetches + caches them.
KC_JWKS_URI = os.getenv(
    "KEYCLOAK_JWKS_URI", f"{KC_ISSUER}/protocol/openid-connect/certs"
)
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "https://meeting-ops.unicorncommander.ai").rstrip("/")
REDIRECT_URI = f"{APP_PUBLIC_URL}/api/auth/sso/uc/callback"

# Cookie the auth dependency reads (see auth/dependencies.py).
SESSION_COOKIE_NAME = os.getenv("UC_SSO_COOKIE_NAME", "mo_uc_session")
SESSION_DAYS = int(os.getenv("UC_SSO_SESSION_DAYS", "7"))

# Logout needs the Keycloak id_token as id_token_hint to actually END the SSO
# session. On prod there's no oauth2-proxy to inject it via the Authorization
# header, so we stash it at login in its own HttpOnly cookie scoped to the auth
# routes; /api/auth/sso-logout reads it back (see auth/routes.py::sso_logout).
IDT_COOKIE_NAME = os.getenv("UC_SSO_IDT_COOKIE_NAME", "mo_uc_idt")
IDT_COOKIE_PATH = "/api/auth"

_STATE_COOKIE = "mo_uc_oidc_state"
_RT_COOKIE = "mo_uc_oidc_rt"
_TRANSIENT_PATH = "/api/auth/sso/uc"

_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    """Process-wide PyJWKClient; caches the Keycloak JWKS (refreshes hourly)."""
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(
            KC_JWKS_URI,
            cache_keys=True,
            lifespan=3600,
            timeout=10,
            # Keycloak's public JWKS is fronted by Cloudflare, which 403s the
            # default ``Python-urllib/x`` User-Agent as a bot. With no UA on the
            # JWKS fetch, ``get_signing_key_from_jwt`` raised, so id_token
            # verification failed ("403 Forbidden") and EVERY SSO login was
            # rejected. A plain identifying UA passes Cloudflare and loads the
            # realm signing keys. (PyJWT >= 2.8 supports ``headers``.)
            headers={"User-Agent": "Meeting-Ops-OIDC/1.0"},
        )
    return _jwks_client


def _verify_id_token(id_token: str) -> dict:
    """Verify a Keycloak id_token: RS256 signature (via JWKS) + iss + aud + exp.

    Replaces ``jwt.get_unverified_claims`` so a token that wasn't actually
    signed by our realm (or is expired / minted for another client) is rejected
    instead of trusted. The token already arrives over the TLS back-channel from
    Keycloak, but this closes the gap where any well-formed JWT would have been
    accepted.

    Raises ``jwt.PyJWTError`` on any verification failure. Does blocking JWKS
    I/O on cache miss, so call it via ``asyncio.to_thread`` from the async
    callback.
    """
    signing_key = _get_jwks_client().get_signing_key_from_jwt(id_token)
    claims = pyjwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        issuer=KC_ISSUER,
        leeway=30,  # tolerate minor backend<->KC clock skew
        options={"require": ["exp", "iss"], "verify_aud": False},
    )
    # Audience: Keycloak id_tokens carry aud=client_id and/or azp=client_id.
    # Accept either so verification is robust to the realm's audience-mapper
    # config (per UC dev: "verify azp (or aud) == client_id").
    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else ([aud] if aud else [])
    if KC_CLIENT_ID not in aud_list and claims.get("azp") != KC_CLIENT_ID:
        raise pyjwt.InvalidAudienceError(
            f"id_token audience mismatch (aud={aud_list} azp={claims.get('azp')}; "
            f"expected {KC_CLIENT_ID})"
        )
    return claims


def _safe_return_to(rt: Optional[str]) -> str:
    """Open-redirect guard: only local single-slash paths."""
    if rt and rt.startswith("/") and not rt.startswith("//"):
        return rt
    return "/"


# Client ids whose uchub-issued access tokens the API accepts as raw Bearer
# credentials. Native apps (the iOS client) do standards OIDC straight against
# Keycloak and present the realm access token — there's no oauth2-proxy header
# and no app session cookie on that path, so the API must trust the realm
# token itself. Keep this list tight: it's an auth surface.
ACCEPTED_BEARER_CLIENT_IDS = frozenset(
    c.strip()
    for c in os.getenv(
        "KEYCLOAK_ACCEPTED_CLIENT_IDS", f"{KC_CLIENT_ID},meeting-ops-ios"
    ).split(",")
    if c.strip()
)


def verify_kc_bearer_token(token: str) -> Optional[dict]:
    """Verify a raw uchub Keycloak *access token* presented as a Bearer.

    Same verification stance as ``_verify_id_token`` (RS256 via realm JWKS,
    iss + exp enforced, 30s leeway) plus two access-token-specific checks:

    - ``typ`` must be Bearer (when present) so a leaked *id_token* can't be
      replayed as an API credential;
    - ``azp``/``aud`` must name one of ``ACCEPTED_BEARER_CLIENT_IDS`` so only
      tokens minted for our own clients authenticate (per UC dev guidance:
      "verify azp (or aud) == client_id").

    Returns the verified claims, or None when this bearer simply isn't a
    valid uchub access token — callers fall through to the other auth
    methods, preserving optional-auth semantics.

    Does blocking JWKS I/O on cache miss; call via ``asyncio.to_thread``
    from async request paths.
    """
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=KC_ISSUER,
            leeway=30,
            options={"require": ["exp", "iss"], "verify_aud": False},
        )
    except pyjwt.PyJWTError:
        return None
    if claims.get("typ") not in (None, "Bearer"):
        return None
    aud = claims.get("aud")
    aud_list = aud if isinstance(aud, list) else ([aud] if aud else [])
    if claims.get("azp") not in ACCEPTED_BEARER_CLIENT_IDS and not (
        ACCEPTED_BEARER_CLIENT_IDS & set(aud_list)
    ):
        logger.warning(
            "uchub bearer rejected: azp=%s aud=%s not in accepted client ids",
            claims.get("azp"), aud_list,
        )
        return None
    return claims


def _login_error(reason: str) -> RedirectResponse:
    return RedirectResponse(
        f"{APP_PUBLIC_URL}/#/login?sso_error={urllib.parse.quote(reason)}",
        status_code=302,
    )


@router.get("/start")
async def uc_sso_start(returnTo: Optional[str] = None):
    """Kick off the Keycloak authorization-code flow."""
    if not KC_CLIENT_SECRET:
        logger.error("UC SSO start invoked but KEYCLOAK_CLIENT_SECRET is unset")
        return _login_error("sso_not_configured")

    state = secrets.token_urlsafe(24)
    params = {
        "response_type": "code",
        "client_id": KC_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid email profile",
        "state": state,
    }
    authorize_url = f"{KC_ISSUER}/protocol/openid-connect/auth?" + urllib.parse.urlencode(params)
    resp = RedirectResponse(authorize_url, status_code=302)
    cookie_kw = dict(httponly=True, secure=True, samesite="lax", max_age=600, path=_TRANSIENT_PATH)
    resp.set_cookie(_STATE_COOKIE, state, **cookie_kw)
    resp.set_cookie(_RT_COOKIE, _safe_return_to(returnTo), **cookie_kw)
    return resp


@router.get("/callback")
async def uc_sso_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Complete the flow: exchange code, provision, set the session cookie."""
    if error:
        logger.warning("UC SSO callback returned error from Keycloak: %s", error)
        return _login_error(error)

    expected_state = request.cookies.get(_STATE_COOKIE)
    return_to = _safe_return_to(request.cookies.get(_RT_COOKIE))
    if not code or not state or not expected_state or state != expected_state:
        logger.warning(
            "UC SSO callback state check failed (code=%s, state_match=%s)",
            bool(code), state == expected_state,
        )
        return _login_error("state_mismatch")

    token_url = f"{KC_ISSUER}/protocol/openid-connect/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": KC_CLIENT_ID,
        "client_secret": KC_CLIENT_SECRET,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            tr = await client.post(
                token_url, data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if tr.status_code != 200:
            logger.error("UC SSO token exchange failed: %s %s", tr.status_code, tr.text[:300])
            return _login_error("token_exchange_failed")
        token_response = tr.json()
        id_token = token_response.get("id_token") or ""
        access_token = token_response.get("access_token")
    except Exception:
        logger.exception("UC SSO token exchange failed")
        return _login_error("token_exchange_failed")

    if not id_token:
        logger.error("UC SSO token response carried no id_token")
        return _login_error("token_exchange_failed")

    # Verify the id_token (RS256 sig via JWKS + iss + aud + exp) rather than
    # trusting unverified claims. Runs off the event loop (blocking JWKS fetch
    # on cache miss).
    try:
        claims = await asyncio.to_thread(_verify_id_token, id_token)
    except pyjwt.PyJWTError as exc:
        logger.warning("UC SSO id_token verification failed: %s", exc)
        return _login_error("token_verification_failed")
    except Exception:
        logger.exception("UC SSO id_token verification error")
        return _login_error("token_verification_failed")

    email = claims.get("email") or claims.get("preferred_username")
    if not email or "@" not in str(email):
        logger.warning("UC SSO: no usable email claim; claim keys=%s", list(claims.keys()))
        return _login_error("no_email")

    groups = parse_group_list(claims.get("groups") or [])
    try:
        user = AuthService.get_or_create_sso_user(
            db,
            email=email,
            username=claims.get("preferred_username"),
            full_name=claims.get("name") or claims.get("preferred_username"),
            is_superuser=is_global_admin(groups),
            groups=groups,
        )
    except Exception:
        logger.exception("UC SSO auto-provision failed for %s", email)
        return _login_error("provision_failed")

    # Mint a session-lifetime app access token (same shape the bearer path
    # decodes: sub=user id, type=access) and hand it back as an HttpOnly
    # cookie. probeSso() in the SPA reads /api/auth/me, which the auth
    # dependency now satisfies from this cookie.
    org_id = user.organizations[0].organization_id if user.organizations else None
    await sync_oc_entitlement_grant(db, user, access_token)
    session_token = create_access_token(
        {"sub": str(user.id), "username": user.username, "email": user.email, "org_id": org_id},
        expires_delta=timedelta(days=SESSION_DAYS),
    )
    resp = RedirectResponse(f"{APP_PUBLIC_URL}{return_to}", status_code=302)
    resp.set_cookie(
        SESSION_COOKIE_NAME, session_token,
        httponly=True, secure=True, samesite="lax",
        max_age=SESSION_DAYS * 86400, path="/",
    )
    # Stash the KC id_token so /api/auth/sso-logout can pass it as id_token_hint
    # and end the Keycloak SSO session (prod has no oauth2-proxy to inject it).
    # Scoped to the auth routes and the same lifetime as the session cookie.
    if id_token:
        resp.set_cookie(
            IDT_COOKIE_NAME, id_token,
            httponly=True, secure=True, samesite="lax",
            max_age=SESSION_DAYS * 86400, path=IDT_COOKIE_PATH,
        )
    resp.delete_cookie(_STATE_COOKIE, path=_TRANSIENT_PATH)
    resp.delete_cookie(_RT_COOKIE, path=_TRANSIENT_PATH)
    logger.info("UC SSO sign-in complete for %s (user_id=%s)", email, user.id)
    return resp
