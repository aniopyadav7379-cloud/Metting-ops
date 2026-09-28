"""Billing endpoints — Checkout Session, Billing Portal, current subscription.

Three routes:

  POST /api/billing/checkout
      Body: {"plan": "pro"}
      Returns: {"url": "https://checkout.stripe.com/..."}
      Creates a Stripe Subscription Checkout session for the logged-in
      user and the requested plan. Frontend redirects window.location
      to the returned URL.

  POST /api/billing/portal
      Returns: {"url": "https://billing.stripe.com/..."}
      Creates a Stripe Billing Portal session so the user can update
      their card / cancel / view invoices. Aaron's cancel UX is
      cancel-at-period-end, configured in the Stripe Dashboard at the
      Portal product level — not here.

  GET /api/billing/subscription
      Returns: {tier, is_founding_member, founding_cohort, subscription_id, status, ...}
      Reports the current subscription state. Frontend uses this for
      the "View existing subscription" link on /pricing and for any
      future "your plan" banner.

All three require auth and surface a 503 (not 500) when Stripe isn't
configured, so the API doesn't look broken on environments where billing
hasn't been wired yet (test sandboxes, offline appliance builds).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from auth.dependencies import get_current_user, get_current_organization
from auth.organization import ActiveOrganization
from auth.models import User
from database.database import get_db
from services import stripe_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/billing", tags=["billing"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CheckoutRequest(BaseModel):
    plan: str = Field(
        default="pro",
        description=(
            "Plan name: 'basic', 'pro', 'suite', or 'enterprise'. "
            "Enterprise returns a 400 'contact sales' message — there's no "
            "public Checkout link for it. Basic / Pro / Suite all produce "
            "live Checkout sessions."
        ),
    )
    billing_cycle: str = Field(
        default="monthly",
        description=(
            "Billing cycle for the chosen plan: 'monthly' or 'annual'. "
            "All three paid tiers (Basic / Pro / Suite) have monthly + "
            "annual options in v3.23.0. Annual saves 2 months on every tier."
        ),
    )
    # Optional return URLs — frontend usually omits and we use defaults.
    success_url: Optional[str] = None
    cancel_url: Optional[str] = None


class CheckoutResponse(BaseModel):
    url: str


class PortalResponse(BaseModel):
    url: str


class SubscriptionResponse(BaseModel):
    tier: str
    is_founding_member: bool
    founding_cohort: Optional[str] = None
    has_subscription: bool
    subscription_id: Optional[str] = None
    status: Optional[str] = None
    price_id: Optional[str] = None
    current_period_end: Optional[str] = None
    cancel_at_period_end: bool = False
    # True when the instance is running Stripe in TEST mode (STRIPE_TEST_MODE=1).
    # The Pricing page shows a "no real charges" badge so a test checkout
    # isn't mistaken for a live one.
    test_mode: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app_base_url() -> str:
    """Default base URL for Stripe redirect targets. Falls back to a
    sensible localhost so dev works without extra config."""
    raw = (
        os.getenv("APP_BASE_URL")
        or os.getenv("FRONTEND_BASE_URL")
        or "http://localhost:7777"
    ).rstrip("/")
    return raw


def _ensure_configured() -> None:
    """Raise 503 if Stripe isn't configured. Routes call this first so
    we don't dump SDK init errors into the response body."""
    if not stripe_client.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not enabled on this instance.",
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    payload: CheckoutRequest,
    current_user: User = Depends(get_current_user),
    active_org: ActiveOrganization = Depends(get_current_organization),
    db: Session = Depends(get_db),
) -> CheckoutResponse:
    """Create a Stripe Checkout session for the current user, upgrading their
    ACTIVE organization (the workspace they're buying for). The org id rides in
    the subscription metadata so the webhook can lift that org's quota plan."""
    _ensure_configured()
    cycle = (payload.billing_cycle or "monthly").lower()
    if cycle not in ("monthly", "annual"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown billing_cycle '{payload.billing_cycle}'. Use 'monthly' or 'annual'.",
        )
    plan = (payload.plan or "").lower()
    # v3.23.0: Suite checkouts are allowed end-to-end even though the
    # cross-app entitlement (Project-Ops / Contact-Ops Pro benefits) goes
    # live only when Brigade federation deploys. Aaron's call: take the
    # money, deliver the Meeting-Ops Pro benefit immediately, deliver the
    # cross-app benefit when federation lands.
    known_plans = {"basic", "pro", "pro_annual", "suite", "enterprise"}
    if plan not in known_plans:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown plan '{payload.plan}'. "
                f"Valid plans: basic, pro, suite, enterprise."
            ),
        )
    # Enterprise has no public Checkout — it's a sales motion. Trip this
    # before tier_to_price_id so the message is the helpful "contact sales"
    # one regardless of whether the env var is unset.
    if plan == "enterprise":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Enterprise is contact-sales only. Email "
                "hello@magicunicorn.dev to start an Enterprise conversation."
            ),
        )
    price_id = stripe_client.tier_to_price_id(plan, billing_cycle=cycle)
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No Stripe price configured for plan '{payload.plan}'.",
        )

    base = _app_base_url()
    success_url = (
        payload.success_url
        or f"{base}/#/sessions?upgrade=success&session_id={{CHECKOUT_SESSION_ID}}"
    )
    cancel_url = payload.cancel_url or f"{base}/#/pricing?upgrade=cancelled"

    # billing-2: an already-subscribed user clicking "Subscribe" again would
    # create a SECOND Stripe subscription and get double-billed. Route them to
    # the Billing Portal (upgrade/downgrade/cancel) instead of a fresh Checkout.
    # Only block truly-live subs — an `incomplete` status is an abandoned
    # checkout, so those users may still start a new one. This server-side guard
    # is authoritative even if the Pricing UI is bypassed.
    try:
        existing_sub = stripe_client.get_active_subscription(current_user)
    except Exception as exc:  # noqa: BLE001
        # Never let a Stripe lookup blip block a legitimate first purchase.
        logger.warning("checkout: active-subscription lookup failed: %s", exc)
        existing_sub = None
    if existing_sub and existing_sub.get("status") in ("active", "trialing", "past_due"):
        try:
            portal_url = stripe_client.create_billing_portal_session(
                current_user, return_url=f"{base}/#/pricing"
            )
            return CheckoutResponse(url=portal_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("checkout: portal redirect for existing sub failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "You already have an active subscription. Manage or change "
                    "your plan from the billing portal."
                ),
            )

    try:
        url = stripe_client.create_checkout_session(
            db=db,
            user=current_user,
            price_id=price_id,
            success_url=success_url,
            cancel_url=cancel_url,
            organization_id=active_org.organization.id,
        )
    except RuntimeError as exc:
        logger.error("checkout: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe is not configured.",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("checkout: Stripe API error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Checkout could not be created.",
        )
    return CheckoutResponse(url=url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PortalResponse:
    """Create a Billing Portal session for the current user."""
    _ensure_configured()
    base = _app_base_url()
    return_url = f"{base}/#/sessions"
    # If the user has no Stripe customer record yet (free user who never
    # checked out), bootstrap one so the Portal opens cleanly.
    if not current_user.stripe_customer_id:
        try:
            stripe_client.get_or_create_stripe_customer(db, current_user)
        except RuntimeError as exc:
            logger.error("portal: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Stripe is not configured.",
            )
    try:
        url = stripe_client.create_billing_portal_session(current_user, return_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("portal: Stripe API error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Billing portal could not be created.",
        )
    return PortalResponse(url=url)


@router.get("/subscription", response_model=SubscriptionResponse)
async def get_subscription(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SubscriptionResponse:
    """Return the user's current subscription state.

    Always returns 200 (with `has_subscription=False`) for users who
    have never paid. Returns Stripe data when configured + an active
    subscription exists.
    """
    base_response = SubscriptionResponse(
        tier=current_user.tier or "free",
        is_founding_member=bool(getattr(current_user, "is_founding_member", False)),
        founding_cohort=getattr(current_user, "founding_cohort", None),
        has_subscription=False,
        test_mode=stripe_client.test_mode(),
    )
    if not stripe_client.is_configured() or not current_user.stripe_customer_id:
        return base_response
    try:
        sub = stripe_client.get_active_subscription(current_user)
    except RuntimeError:
        return base_response
    except Exception as exc:  # noqa: BLE001
        logger.warning("subscription: Stripe lookup failed: %s", exc)
        return base_response
    if sub is None:
        return base_response
    end = sub.get("current_period_end")
    if isinstance(end, (int, float)):
        try:
            end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            end_iso = None
    else:
        end_iso = None
    return SubscriptionResponse(
        tier=sub.get("tier") or current_user.tier or "free",
        is_founding_member=bool(getattr(current_user, "is_founding_member", False)),
        founding_cohort=getattr(current_user, "founding_cohort", None),
        has_subscription=True,
        subscription_id=sub.get("subscription_id"),
        status=sub.get("status"),
        price_id=sub.get("price_id"),
        current_period_end=end_iso,
        cancel_at_period_end=bool(sub.get("cancel_at_period_end")),
        test_mode=stripe_client.test_mode(),
    )
