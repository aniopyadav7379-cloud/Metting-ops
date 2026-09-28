import asyncio
import logging
import os
from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer, APIKeyHeader
from sqlalchemy.orm import Session
from jose import JWTError

from database.database import get_db
from auth.models import User
from auth.oidc_sso import verify_kc_bearer_token
from auth.organization import ActiveOrganization, is_global_admin, parse_group_list, resolve_active_organization
from auth.pat import TOKEN_PREFIX as PAT_PREFIX, resolve_pat
from auth.proxy_trust import forward_auth_trusted
from auth.service import AuthService
from auth.utils import decode_token, check_permission

logger = logging.getLogger(__name__)

# OAuth2 scheme for JWT tokens
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

# API Key header scheme
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _first_email(*candidates: Optional[str]) -> Optional[str]:
    for value in candidates:
        if value and "@" in value:
            return value.strip()
    return None


def _get_sso_groups(request: Request) -> list[str]:
    raw_groups = (
        request.headers.get("X-Auth-Request-Groups")
        or request.headers.get("X-Forwarded-Groups")
        or ""
    )
    return parse_group_list(g.strip() for g in raw_groups.split(",") if g.strip())

async def get_current_user_optional(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    api_key: Optional[str] = Depends(api_key_header),
    db: Session = Depends(get_db)
) -> Optional[User]:
    """Get current user from SSO header, JWT token, or API key (optional).

    Order of trust:
      1. oauth2-proxy forward-auth headers (X-Auth-Request-Email)
      2. JWT bearer token
      3. X-API-Key header
    SSO is checked first so Keycloak-authed users are auto-provisioned.
    """
    user = None

    # Try SSO forward-auth headers first (oauth2-proxy / Keycloak).
    # Keycloak doesn't always emit a discrete email claim; preferred_username
    # often holds it, so we fall through several headers and accept the first
    # value that looks like an email.
    #
    # SECURITY: these headers are only honoured when the request crossed the
    # trusted proxy (forward_auth_trusted checks the X-Proxy-Auth secret). On a
    # shared docker network an untrusted co-tenant could otherwise forge the
    # identity + groups headers → auth bypass + superuser escalation. The gate
    # is FAIL-CLOSED: forward-auth headers are honoured only when a shared
    # secret is configured AND the request carries a matching X-Proxy-Auth
    # (Traefik-injected). Otherwise they're ignored and the caller falls through
    # to JWT / cookie / PAT / API-key below — so native-OIDC (no secret) is
    # unaffected and a forged header authenticates no one.
    sso_email = (
        _first_email(
            request.headers.get("X-Auth-Request-Email"),
            request.headers.get("X-Forwarded-Email"),
            request.headers.get("X-Auth-Request-Preferred-Username"),
            request.headers.get("X-Forwarded-Preferred-Username"),
            request.headers.get("X-Auth-Request-User"),
            request.headers.get("X-Forwarded-User"),
        )
        if forward_auth_trusted(request.headers)
        else None
    )
    if sso_email:
        sso_username = (
            request.headers.get("X-Auth-Request-Preferred-Username")
            or request.headers.get("X-Auth-Request-User")
            or request.headers.get("X-Forwarded-User")
        )
        sso_groups = _get_sso_groups(request)
        request.state.sso_groups = sso_groups
        is_admin = is_global_admin(sso_groups)
        try:
            user = AuthService.get_or_create_sso_user(
                db,
                email=sso_email,
                username=sso_username,
                full_name=sso_username,
                is_superuser=is_admin,
                groups=sso_groups,
            )
        except Exception:
            # Don't swallow silently — auth-loop debugging is impossible
            # when this is invisible (see Shafen 2026-05-18: full-email
            # username + a sanitize regex that rejects '@'/'.').
            logger.exception("SSO auto-provision failed for email=%s username=%s", sso_email, sso_username)
            user = None

    # Try Personal Access Token bearer auth before JWT decoding. PATs are
    # intentionally prefix-scoped so legacy JWT/SSO behavior is unchanged.
    if not user and token and token.startswith(PAT_PREFIX):
        try:
            user = resolve_pat(db, plaintext=token)
        except Exception:
            logger.exception("PAT resolution failed for prefix=%s", token[:12])
            user = None

    # Try JWT token
    if not user and token:
        try:
            payload = decode_token(token)
            if payload and payload.get("type") == "access":
                user_id = payload.get("sub")
                if user_id:
                    user = AuthService.get_user_by_id(db, int(user_id))
        except (JWTError, ValueError):
            pass

    # Try a raw uchub Keycloak access token. Native clients (the iOS app)
    # sign in via standards OIDC straight against Keycloak and call the API
    # with the realm access token — no oauth2-proxy headers, no app session
    # cookie exist on that path (the App Store build 401'd here on launch
    # day: iOS shipped SSO in 388bdce but nothing server-side accepted the
    # token). verify_kc_bearer_token enforces RS256-via-JWKS + iss + exp +
    # azp/aud ∈ accepted client ids; on success the user is auto-provisioned
    # through the exact same path as every other SSO login (personal org,
    # free tier), so web/proxy/native logins all land on one account model.
    if not user and token and not token.startswith(PAT_PREFIX):
        try:
            kc_claims = await asyncio.to_thread(verify_kc_bearer_token, token)
        except Exception:
            logger.exception("uchub bearer verification errored")
            kc_claims = None
        if kc_claims:
            kc_email = _first_email(
                kc_claims.get("email"), kc_claims.get("preferred_username")
            )
            if kc_email:
                kc_groups = parse_group_list(kc_claims.get("groups") or [])
                request.state.sso_groups = kc_groups
                try:
                    user = AuthService.get_or_create_sso_user(
                        db,
                        email=kc_email,
                        username=kc_claims.get("preferred_username"),
                        full_name=kc_claims.get("name")
                        or kc_claims.get("preferred_username"),
                        is_superuser=is_global_admin(kc_groups),
                        groups=kc_groups,
                    )
                except Exception:
                    logger.exception(
                        "uchub bearer auto-provision failed for email=%s", kc_email
                    )
                    user = None
            else:
                logger.warning(
                    "uchub bearer verified but carried no usable email claim; keys=%s",
                    list(kc_claims.keys()),
                )

    # Try the app-level UC-SSO session cookie set by auth/oidc_sso.py after a
    # Keycloak login. Same JWT shape as the bearer access token (type=access,
    # sub=user id), so it decodes through the identical path.
    if not user:
        cookie_token = request.cookies.get(os.getenv("UC_SSO_COOKIE_NAME", "mo_uc_session"))
        if cookie_token:
            try:
                payload = decode_token(cookie_token)
                if payload and payload.get("type") == "access":
                    user_id = payload.get("sub")
                    if user_id:
                        user = AuthService.get_user_by_id(db, int(user_id))
            except (JWTError, ValueError):
                pass

    # Try API key if no valid JWT
    if not user and api_key:
        user = AuthService.get_user_by_api_key(db, api_key)

    # Check if user is active
    if user and not user.is_active:
        return None

    return user

async def get_current_user(
    user: Optional[User] = Depends(get_current_user_optional)
) -> User:
    """Get current user (required)"""
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user

async def get_current_active_user(
    current_user: User = Depends(get_current_user)
) -> User:
    """Get current active user"""
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user"
        )
    return current_user


async def get_current_organization(
    request: Request,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
) -> ActiveOrganization:
    """Resolve the caller's active organization for the current request."""
    return resolve_active_organization(db, request, current_user)

def require_permission(permission: str):
    """Dependency to require a specific permission"""
    async def permission_checker(
        current_user: User = Depends(get_current_active_user),
        active_org: ActiveOrganization = Depends(get_current_organization),
    ):
        role = "superuser" if current_user.is_superuser else active_org.role
        if not check_permission(role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required: {permission}"
            )
        
        return current_user
    
    return permission_checker

def require_role(required_role: str):
    """Dependency to require a specific role or higher"""
    async def role_checker(
        current_user: User = Depends(get_current_active_user),
        active_org: ActiveOrganization = Depends(get_current_organization),
    ):
        role = "superuser" if current_user.is_superuser else active_org.role
        from auth.config import ROLE_HIERARCHY
        if role not in ROLE_HIERARCHY or required_role not in ROLE_HIERARCHY[role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient role. Required: {required_role}, Current: {role}"
            )
        
        return current_user
    
    return role_checker

# Convenience dependencies for common roles
require_admin = require_role("admin")
require_manager = require_role("manager")
require_user = require_role("user")

# Audit logging dependency
async def log_request(
    request: Request,
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: Session = Depends(get_db)
):
    """Log API requests for audit trail"""
    if current_user:
        # Get IP address
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")
        
        # Log the request
        AuthService.log_action(
            db,
            current_user.id,
            f"api_request:{request.method}:{request.url.path}",
            ip_address=ip_address,
            user_agent=user_agent,
            details={
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.query_params)
            }
        )
