"""Per-org integration configuration API (v3.19.0).

Surface:
  GET    /api/integrations/me                          -> list all integrations (masked)
  GET    /api/integrations/me/{integration}            -> single integration (masked)
  PUT    /api/integrations/me/{integration}            -> upsert config (admin only)
  DELETE /api/integrations/me/{integration}            -> disable + clear creds (admin only)
  POST   /api/integrations/me/{integration}/test       -> best-effort credentials probe

Secrets handling:
  * API keys arrive in PUT bodies as cleartext; we encrypt with Fernet
    (services.providers.crypto) before writing to JSONB.
  * Every GET response masks the secret — only the last 4 chars are
    shown ("api_key_last4"), with a boolean ``has_api_key`` flag for
    UI logic. The cleartext is NEVER returned by any GET.
  * The /test endpoint reads the encrypted key out of the row,
    decrypts it server-side, fires a single HEAD/GET at a known health
    URL, and returns {"ok": True} or {"ok": False, "error": "..."}.
    The cleartext key never traverses the response boundary.

Authn/Authz:
  * All routes require an authenticated user (get_current_user) + an
    active org (get_current_organization).
  * PUT / DELETE / POST .../test require org admin (or superuser).
    GET is readable by any org member but returns masked secrets.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from auth.dependencies import get_current_organization, get_current_user
from auth.models import Organization, User
from auth.organization import ActiveOrganization
from database.database import get_db
from services.integrations.org_config import (
    VALID_INTEGRATION_KEYS,
    empty_integration_block,
    INTEGRATION_BRIGADE,
    INTEGRATION_PROJECT_OPS,
    INTEGRATION_CONTACT_OPS,
    INTEGRATION_ACCOUNTING_OPS,
    INTEGRATION_STABLE,
    INTEGRATION_JIRA,
    INTEGRATION_TODOIST,
    INTEGRATION_CLICKUP,
    INTEGRATION_NOTION,
    INTEGRATION_CONFLUENCE,
    INTEGRATION_SLACK,
    INTEGRATION_TEAMS,
    INTEGRATION_EMAIL,
)
from services.providers.crypto import decrypt_api_key, encrypt_api_key


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/integrations/me", tags=["Org Integrations"])


# ---------------------------------------------------------------------------
# Pydantic shapes (request/response)
# ---------------------------------------------------------------------------


class IntegrationView(BaseModel):
    """Masked view of one integration's config — safe for GET responses.

    Never include cleartext api_key. ``api_key_last4`` is the last 4
    chars of the cleartext (post-decrypt) for the UI to render
    "•••• 4f7a"; ``has_api_key`` is the boolean the UI binds to.
    """

    key: str
    enabled: bool
    api_base_url: str = ""
    has_api_key: bool = False
    api_key_last4: str = ""
    # Integration-specific extras (already safe, no secrets).
    extra: dict[str, Any] = Field(default_factory=dict)


class IntegrationUpdate(BaseModel):
    """PUT body. All fields optional — only provided ones are written.

    api_key:
      - None / omitted -> leave existing ciphertext untouched.
      - "" (empty string) -> clear the stored key (disable creds).
      - any other string -> encrypt + store.
    """

    enabled: Optional[bool] = None
    api_base_url: Optional[str] = None
    api_key: Optional[str] = None
    # Brigade-only
    tenancy_mode: Optional[str] = None
    shared_agent_id: Optional[str] = None
    # Project-Ops-only
    default_project_number: Optional[str] = None
    # Whether extracted action items are auto-pushed to PO as tasks on
    # meeting completion. Default OFF (opt-in) to avoid task noise; the
    # writer only fires the automatic push when this is True.
    auto_push_action_items: Optional[bool] = None
    # Email-only (SMTP connection details — the account password itself
    # travels through the shared `api_key` field above, same
    # encrypted-at-rest handling as every other provider's secret).
    smtp_host: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_from_address: Optional[str] = None
    smtp_use_tls: Optional[bool] = None


class TestResult(BaseModel):
    ok: bool
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_admin(current_user: User, active_org: ActiveOrganization) -> None:
    """403 unless the caller is org admin or a superuser."""
    if current_user.is_superuser:
        return
    if active_org.role in ("admin", "superuser"):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only org admins can edit integrations",
    )


def _validate_key(integration_key: str) -> None:
    if integration_key not in VALID_INTEGRATION_KEYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown integration {integration_key!r}; must be one of "
                f"{list(VALID_INTEGRATION_KEYS)}"
            ),
        )


_INTERNAL_INTEGRATION_HOSTS = {
    "unicorn-brigade",
    "project-ops-backend",
    "contact-ops-backend",
    "accounting-ops-backend",
    "stable-bridge",
}


def _validated_integration_base_url(raw_url: str) -> str:
    """Validate an admin-supplied upstream URL before attaching credentials.

    Public integrations must use HTTPS and resolve only to globally-routable
    addresses.  Plain HTTP is allowed solely for named service containers on
    the shared Docker network. This prevents the credential-test feature from
    becoming an authenticated SSRF probe into loopback, cloud metadata, or
    other private network services.
    """
    value = raw_url.strip().rstrip("/")
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid API base URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        not host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.scheme not in {"http", "https"}
    ):
        raise HTTPException(
            status_code=400,
            detail="API base URL must be an HTTP(S) origin without credentials, query, or fragment",
        )

    configured_internal = {
        item.strip().lower()
        for item in os.getenv("INTEGRATION_INTERNAL_HOST_ALLOWLIST", "").split(",")
        if item.strip()
    }
    internal_hosts = _INTERNAL_INTEGRATION_HOSTS | configured_internal
    if host in internal_hosts:
        return value
    if parsed.scheme != "https":
        raise HTTPException(
            status_code=400,
            detail="Public integration URLs must use HTTPS",
        )

    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise HTTPException(status_code=400, detail="Private integration hosts are not allowed")
        try:
            addresses = [
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            ]
        except (socket.gaierror, ValueError) as exc:
            raise HTTPException(
                status_code=400,
                detail="Integration hostname could not be resolved",
            ) from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise HTTPException(
            status_code=400,
            detail="Private, loopback, link-local, and reserved integration addresses are not allowed",
        )
    return value


def _all_blocks(org: Organization) -> dict[str, dict[str, Any]]:
    """Return the full integrations dict, filling in empty blocks for any
    integration the org hasn't touched yet. Reads only — never mutates."""
    raw = org.integrations or {}
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, dict[str, Any]] = {}
    for key in VALID_INTEGRATION_KEYS:
        existing = raw.get(key)
        if isinstance(existing, dict):
            # Merge stored block over empty defaults so any fields the UI
            # expects but were never persisted come back as empty strings
            # rather than missing keys.
            merged = empty_integration_block(key)
            merged.update(existing)
            out[key] = merged
        else:
            out[key] = empty_integration_block(key)
    return out


def _mask_block(integration_key: str, block: dict[str, Any]) -> IntegrationView:
    """Convert a stored block into the safe-to-serialize view.

    Pulls the last-4 chars off the cleartext (post-decrypt). If
    decryption fails we still return has_api_key=True with last4=""
    so the UI shows "credentials present but unreadable" and the admin
    can re-enter them.
    """
    ciphertext = (block.get("api_key_encrypted") or "").strip()
    has_key = bool(ciphertext)
    last4 = ""
    if has_key:
        try:
            cleartext = decrypt_api_key(ciphertext)
            last4 = cleartext[-4:] if cleartext else ""
        except Exception as exc:  # noqa: BLE001
            # NEVER include the ciphertext or the exception body in the
            # response — log + carry on with last4="".
            logger.warning(
                "integrations_org: failed to decrypt key for %s "
                "(corrupt or wrong PROVIDER_ENCRYPTION_KEY): %s",
                integration_key,
                exc,
            )
    extra: dict[str, Any] = {}
    if integration_key == INTEGRATION_BRIGADE:
        extra["tenancy_mode"] = block.get("tenancy_mode") or "shared"
        extra["shared_agent_id"] = block.get("shared_agent_id") or ""
    elif integration_key == INTEGRATION_PROJECT_OPS:
        extra["default_project_number"] = block.get("default_project_number") or ""
        extra["auto_push_action_items"] = bool(
            block.get("auto_push_action_items", False)
        )
    elif integration_key == INTEGRATION_EMAIL:
        extra["smtp_host"] = block.get("smtp_host") or ""
        extra["smtp_port"] = block.get("smtp_port") or 587
        extra["smtp_username"] = block.get("smtp_username") or ""
        extra["smtp_from_address"] = block.get("smtp_from_address") or ""
        extra["smtp_use_tls"] = bool(block.get("smtp_use_tls", True))
    return IntegrationView(
        key=integration_key,
        enabled=bool(block.get("enabled")),
        api_base_url=block.get("api_base_url") or "",
        has_api_key=has_key,
        api_key_last4=last4,
        extra=extra,
    )


def _write_block(
    org: Organization, integration_key: str, new_block: dict[str, Any]
) -> None:
    """Atomically replace one integration block on the org. Reassigns the
    parent dict (rather than mutating in place) so SQLAlchemy reliably
    detects the JSONB change on commit."""
    current = dict(org.integrations or {})
    current[integration_key] = new_block
    org.integrations = current
    # Belt-and-suspenders: some SQLAlchemy/Postgres combos under JSONB
    # don't pick up nested mutations without flag_modified.
    try:
        flag_modified(org, "integrations")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[IntegrationView])
async def list_integrations(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> list[IntegrationView]:
    """Return the current org's integrations with secrets masked.

    Visible to any authenticated org member; the UI gates the edit
    controls on ``role_in_active_org === "admin"`` itself.
    """
    org = (
        db.query(Organization)
        .filter(Organization.id == active_org.organization.id)
        .first()
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Active organization not found")
    blocks = _all_blocks(org)
    return [_mask_block(k, b) for k, b in blocks.items()]


@router.get("/{integration}", response_model=IntegrationView)
async def get_integration(
    integration: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> IntegrationView:
    _validate_key(integration)
    org = (
        db.query(Organization)
        .filter(Organization.id == active_org.organization.id)
        .first()
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Active organization not found")
    block = _all_blocks(org)[integration]
    return _mask_block(integration, block)


@router.put("/{integration}", response_model=IntegrationView)
async def update_integration(
    integration: str,
    payload: IntegrationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> IntegrationView:
    _validate_key(integration)
    _ensure_admin(current_user, active_org)

    org = (
        db.query(Organization)
        .filter(Organization.id == active_org.organization.id)
        .first()
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Active organization not found")

    blocks = _all_blocks(org)
    block = dict(blocks[integration])

    if payload.enabled is not None:
        block["enabled"] = bool(payload.enabled)
    if payload.api_base_url is not None:
        supplied_url = payload.api_base_url.strip()
        block["api_base_url"] = (
            _validated_integration_base_url(supplied_url)
            if supplied_url
            else ""
        )
    # api_key handling:
    #   - None: leave existing encrypted blob alone.
    #   - "":   clear the credential.
    #   - else: encrypt + store.
    if payload.api_key is not None:
        if payload.api_key == "":
            block["api_key_encrypted"] = ""
        else:
            block["api_key_encrypted"] = encrypt_api_key(payload.api_key)

    if integration == INTEGRATION_BRIGADE:
        if payload.tenancy_mode is not None:
            mode = payload.tenancy_mode.strip().lower() or "shared"
            if mode not in ("shared", "per_org_graph", "per_org_instance"):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "tenancy_mode must be one of: shared, "
                        "per_org_graph, per_org_instance"
                    ),
                )
            block["tenancy_mode"] = mode
        if payload.shared_agent_id is not None:
            block["shared_agent_id"] = payload.shared_agent_id.strip()
    elif integration == INTEGRATION_PROJECT_OPS:
        if payload.default_project_number is not None:
            block["default_project_number"] = payload.default_project_number.strip()
        if payload.auto_push_action_items is not None:
            block["auto_push_action_items"] = bool(payload.auto_push_action_items)
    elif integration == INTEGRATION_EMAIL:
        if payload.smtp_host is not None:
            block["smtp_host"] = payload.smtp_host.strip()
        if payload.smtp_port is not None:
            block["smtp_port"] = int(payload.smtp_port)
        if payload.smtp_username is not None:
            block["smtp_username"] = payload.smtp_username.strip()
        if payload.smtp_from_address is not None:
            block["smtp_from_address"] = payload.smtp_from_address.strip()
        if payload.smtp_use_tls is not None:
            block["smtp_use_tls"] = bool(payload.smtp_use_tls)

    _write_block(org, integration, block)
    db.commit()
    db.refresh(org)

    # NEVER log the cleartext key — only that an update happened.
    logger.info(
        "integrations_org update org=%s integration=%s enabled=%s by_user=%s",
        active_org.organization.id,
        integration,
        block.get("enabled"),
        current_user.id,
    )

    return _mask_block(integration, block)


@router.delete("/{integration}", response_model=IntegrationView)
async def delete_integration(
    integration: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> IntegrationView:
    """Disable + clear credentials for one integration.

    Sets enabled=False and api_key_encrypted="" but leaves other fields
    (api_base_url, tenancy_mode, default_project_number) intact so the
    admin can re-enable later without re-entering URLs.
    """
    _validate_key(integration)
    _ensure_admin(current_user, active_org)

    org = (
        db.query(Organization)
        .filter(Organization.id == active_org.organization.id)
        .first()
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Active organization not found")

    blocks = _all_blocks(org)
    block = dict(blocks[integration])
    block["enabled"] = False
    block["api_key_encrypted"] = ""
    _write_block(org, integration, block)
    db.commit()
    db.refresh(org)

    logger.info(
        "integrations_org delete org=%s integration=%s by_user=%s",
        active_org.organization.id,
        integration,
        current_user.id,
    )

    return _mask_block(integration, block)


# ---------------------------------------------------------------------------
# Connectivity probe
# ---------------------------------------------------------------------------


# Per-integration health endpoints. These are intentionally NOT mutating
# — they're best-effort GET-style probes to a known route. If an upstream
# changes its shape, the probe just returns ok=False; we don't try to be
# clever about validating arbitrary API surfaces.
_PROBE_PATHS = {
    INTEGRATION_BRIGADE: "/api/v1/knowledge/stats",
    INTEGRATION_PROJECT_OPS: "/api/v1/organizations/me",
    INTEGRATION_CONTACT_OPS: "/health",
    INTEGRATION_ACCOUNTING_OPS: "/health",
    INTEGRATION_STABLE: "/health",
    INTEGRATION_JIRA: "/rest/api/3/myself",
    INTEGRATION_TODOIST: "/rest/v2/projects",
    INTEGRATION_CLICKUP: "/api/v2/user",
    INTEGRATION_NOTION: "/v1/users/me",
    INTEGRATION_CONFLUENCE: "/wiki/rest/api/space",
    # Slack/Teams (webhook URLs) and Email (SMTP) don't fit a GET-probe
    # shape — they're intentionally absent here and use their own
    # dedicated test endpoints in api/action_item_integrations.py.
}

# Auth header style per integration. Brigade uses X-API-Key; everyone
# else uses Bearer. Keeps the probe code simple.
_AUTH_HEADER_STYLE = {
    INTEGRATION_BRIGADE: "x-api-key",
    INTEGRATION_PROJECT_OPS: "bearer",
    INTEGRATION_CONTACT_OPS: "bearer",
    INTEGRATION_ACCOUNTING_OPS: "bearer",
    INTEGRATION_STABLE: "bearer",
    INTEGRATION_JIRA: "basic",  # Atlassian API tokens use HTTP Basic (email:token), not Bearer
    INTEGRATION_TODOIST: "bearer",
    INTEGRATION_CLICKUP: "raw",  # ClickUp sends the token as-is in the Authorization header, no "Bearer " prefix
    INTEGRATION_NOTION: "bearer",
    INTEGRATION_CONFLUENCE: "basic",
}


@router.post("/{integration}/test", response_model=TestResult)
async def test_integration(
    integration: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
) -> TestResult:
    """Probe upstream with the stored creds.

    Reads the encrypted key from the DB, decrypts in-process, fires one
    request to a known health/identity endpoint, and returns ok/fail.
    The cleartext key never leaves the function frame. Errors are
    sanitized to a short string — never include response bodies that
    might echo the key.
    """
    _validate_key(integration)
    _ensure_admin(current_user, active_org)

    org = (
        db.query(Organization)
        .filter(Organization.id == active_org.organization.id)
        .first()
    )
    if org is None:
        raise HTTPException(status_code=404, detail="Active organization not found")
    block = _all_blocks(org)[integration]

    if not block.get("enabled"):
        return TestResult(ok=False, error="Integration is disabled")
    ciphertext = (block.get("api_key_encrypted") or "").strip()
    if not ciphertext:
        return TestResult(ok=False, error="No API key configured")
    base_url = (block.get("api_base_url") or "").strip()
    if not base_url:
        return TestResult(ok=False, error="No API base URL configured")
    try:
        base_url = _validated_integration_base_url(base_url)
    except HTTPException as exc:
        return TestResult(ok=False, error=str(exc.detail))

    try:
        cleartext = decrypt_api_key(ciphertext)
    except Exception:  # noqa: BLE001
        return TestResult(
            ok=False,
            error="Stored API key could not be decrypted (re-enter it)",
        )

    if integration not in _PROBE_PATHS:
        return TestResult(ok=False, error=f"'{integration}' does not support automatic connection testing yet (webhook/SMTP-based integrations are verified by sending a real test message/email instead)")

    path = _PROBE_PATHS[integration]
    style = _AUTH_HEADER_STYLE[integration]
    headers = {"Accept": "application/json"}
    if style == "bearer":
        headers["Authorization"] = f"Bearer {cleartext}"
    elif style == "basic":
        # Atlassian Cloud (Jira/Confluence) API tokens authenticate via
        # HTTP Basic as "account-email:api-token" — store the API key
        # field as "you@company.com:ATATT3xFfGF0..." for these two.
        import base64

        headers["Authorization"] = f"Basic {base64.b64encode(cleartext.encode()).decode()}"
    elif style == "raw":
        # ClickUp sends its personal API token as-is (no scheme prefix).
        headers["Authorization"] = cleartext
    else:
        headers["X-API-Key"] = cleartext

    url = f"{base_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            resp = await client.get(url, headers=headers)
    except httpx.ConnectError:
        return TestResult(ok=False, error="Could not connect to upstream")
    except httpx.ReadTimeout:
        return TestResult(ok=False, error="Upstream timed out")
    except Exception as exc:  # noqa: BLE001
        # Sanitized — type name only, no body (which could echo creds).
        return TestResult(ok=False, error=f"Request failed ({type(exc).__name__})")

    if 200 <= resp.status_code < 300:
        return TestResult(ok=True)
    if resp.status_code in (401, 403):
        return TestResult(
            ok=False, error=f"Authentication rejected (HTTP {resp.status_code})"
        )
    if resp.status_code == 404:
        # 404 on the probe path is "service up, route differs" — for
        # most of our upstreams that still means the creds are valid;
        # but we surface it so the admin can fix the URL.
        return TestResult(
            ok=False,
            error=f"Probe path not found (HTTP 404); check API base URL",
        )
    return TestResult(ok=False, error=f"Upstream returned HTTP {resp.status_code}")


__all__ = ["router"]
