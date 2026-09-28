"""Login-time sync for Ops-Center Meeting-Ops entitlements.

Meeting-Ops only gates on local user.tier + org.plan today. This module adds an
opt-in, fail-open consumption path that can upgrade a user at Unicorn Commander
SSO login when Ops-Center has granted their org a paid Meeting-Ops entitlement.

Posture:
  - Dormant by default: no ``MEETING_OPS_ENTITLEMENT_URL`` means no network call
    and no behavior change.
  - Fail-open: any HTTP / parse / eligibility error returns without mutating the
    local tier or blocking login.
  - Upgrade-only: only paid tiers with ``meeting_ops_access`` trigger the local
    comp helper. Free/unknown tiers are ignored.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from auth.invite_codes import comp_personal_org_to_pro
from auth.models import User

logger = logging.getLogger(__name__)

_USER_AGENT = "Meeting-Ops-OC-Entitlements/1.0 (+https://unicorncommander.ai)"
_DEFAULT_HTTP_TIMEOUT = 3.0
_DEFAULT_COMP_DAYS = 32


def _entitlement_url() -> str:
    return os.getenv("MEETING_OPS_ENTITLEMENT_URL", "").strip()


def _http_timeout_seconds() -> float:
    raw = os.getenv("MEETING_OPS_ENTITLEMENT_HTTP_TIMEOUT", "").strip()
    if not raw:
        return _DEFAULT_HTTP_TIMEOUT
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid MEETING_OPS_ENTITLEMENT_HTTP_TIMEOUT=%r; using default %.1fs",
            raw,
            _DEFAULT_HTTP_TIMEOUT,
        )
        return _DEFAULT_HTTP_TIMEOUT


def _comp_days() -> int:
    raw = os.getenv("MEETING_OPS_ENTITLEMENT_COMP_DAYS", "").strip()
    if not raw:
        return _DEFAULT_COMP_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid MEETING_OPS_ENTITLEMENT_COMP_DAYS=%r; using default %s",
            raw,
            _DEFAULT_COMP_DAYS,
        )
        return _DEFAULT_COMP_DAYS
    return max(1, days)


def _is_paid_tier(tier: Any) -> bool:
    return tier not in (None, "", "free")


async def fetch_oc_entitlements(access_token: Optional[str]) -> dict[str, Any] | None:
    """Fetch the OC entitlement payload for the caller's Keycloak bearer.

    Returns the parsed JSON object on success, or ``None`` when the feature is
    dormant or any request / parsing / status error occurs.
    """
    url = _entitlement_url()
    if not url:
        logger.info("OC entitlement sync dormant: MEETING_OPS_ENTITLEMENT_URL unset")
        return None
    if not access_token:
        logger.warning("OC entitlement sync skipped: no Keycloak access token available")
        return None

    timeout = _http_timeout_seconds()
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("entitlements response is not a JSON object")
        return payload
    except httpx.HTTPStatusError as exc:
        body = " ".join(exc.response.text.split())[:200]
        logger.warning(
            "OC entitlement fetch failed with HTTP %s: %s",
            exc.response.status_code,
            body,
        )
    except (httpx.TimeoutException, httpx.RequestError, ValueError, TypeError) as exc:
        logger.warning("OC entitlement fetch failed: %s", exc)
    except Exception:
        logger.exception("OC entitlement fetch failed unexpectedly")
    return None


async def sync_oc_entitlement_grant(
    db: Session,
    user: User,
    access_token: Optional[str],
) -> None:
    """Upgrade the local Meeting-Ops tier when OC grants a paid entitlement."""
    try:
        payload = await fetch_oc_entitlements(access_token)
        if not payload:
            return

        entitlements = payload.get("entitlements")
        tier = payload.get("tier")
        if not isinstance(entitlements, list):
            logger.warning(
                "OC entitlement payload missing entitlements list for user_id=%s",
                user.id,
            )
            return
        if "meeting_ops_access" not in entitlements or not _is_paid_tier(tier):
            logger.info(
                "OC entitlement sync not eligible for user_id=%s (tier=%r)",
                user.id,
                tier,
            )
            return

        comp_personal_org_to_pro(db, user, tier="pro", days=_comp_days())
        logger.info(
            "OC entitlement sync upgraded user_id=%s to pro from tier=%r",
            user.id,
            tier,
        )
    except Exception:
        logger.exception(
            "OC entitlement sync failed for user_id=%s; leaving local tier untouched",
            user.id,
        )

