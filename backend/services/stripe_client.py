"""Thin wrapper around the Stripe Python SDK.

Centralizes:
  - API key loading (TEST keys only in dev; live keys gated behind an
    explicit deploy step — see deploy/bigboy/.env.bigboy.example).
  - Customer get-or-create (one Stripe customer per local user, link
    stored on `User.stripe_customer_id`).
  - Checkout Session creation (the link the Pricing page redirects to).
  - Billing Portal Session creation (so existing customers can update
    cards / cancel without us building those screens).

Why a thin wrapper instead of calling `stripe` directly in routes:
  - Single place that imports `stripe` (the lib is optional in tests
    via the stub fallback below; webhook tests exercise signature
    verification through this module too).
  - Single place that sets `stripe.api_key` (avoids a per-route assign).
  - Customer dedup logic lives here so /billing/checkout, /billing/portal,
    and the `customer.created` webhook all stay consistent.

The module is import-safe even when the `stripe` package isn't installed
or `STRIPE_API_KEY` is unset — every public function raises a clear
RuntimeError instead of an ImportError or AttributeError. That keeps the
billing router optional in environments that don't have Stripe wired yet
(test sandboxes, offline appliance builds).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from sqlalchemy.orm import Session

from auth.models import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy import + config — keep billing optional when the lib isn't installed.
# ---------------------------------------------------------------------------

_stripe_module: Optional[Any] = None
_api_key_set = False


# ---------------------------------------------------------------------------
# Test-mode switch — one flag flips the WHOLE billing subsystem to Stripe
# test keys/prices/webhook secret without touching the live config.
# ---------------------------------------------------------------------------


def _test_mode() -> bool:
    """True iff billing runs against Stripe TEST keys.

    Set STRIPE_TEST_MODE=1 (or true/yes/on) and every STRIPE_<X> env read in
    this module transparently prefers STRIPE_TEST_<X> (api key, webhook
    secret, publishable key, all price IDs). The live STRIPE_* vars stay
    configured but dormant, so flipping the flag back is a one-liner and we
    never delete production credentials to test.

    Safety: in test mode _stripe() additionally REFUSES any resolved secret
    key that isn't an `sk_test_` key, so a half-configured node
    (STRIPE_TEST_MODE=1 but STRIPE_TEST_API_KEY unset) goes INERT rather than
    silently falling back to the live key and charging a real card.
    """
    return os.getenv("STRIPE_TEST_MODE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _env(name: str) -> str:
    """Read a STRIPE_<X> config var for the active mode.

    In test mode, prefers STRIPE_TEST_<X> (falling back to the plain var only
    if the test variant is unset). In live mode, reads the plain var. Every
    Stripe credential / price-id lookup in this module routes through here so
    the test-mode flag is the single switch.
    """
    if _test_mode() and name.startswith("STRIPE_"):
        test_val = os.getenv("STRIPE_TEST_" + name[len("STRIPE_"):], "").strip()
        if test_val:
            return test_val
    return os.getenv(name, "").strip()


def test_mode() -> bool:
    """Public accessor for the test-mode flag (frontend badge / health)."""
    return _test_mode()


def webhook_secret() -> str:
    """The Stripe webhook signing secret for the active mode (test/live).

    Routes through the test-mode switch so the webhook verifies against
    STRIPE_TEST_WEBHOOK_SECRET when STRIPE_TEST_MODE is on.
    """
    return _env("STRIPE_WEBHOOK_SECRET")


def publishable_key() -> str:
    """The publishable key for the active mode. Not needed for redirect
    Checkout (the backend creates the session), but exposed for the
    test-mode UI badge + any future Stripe.js usage."""
    return _env("STRIPE_PUBLISHABLE_KEY")


def _stripe():
    """Import the `stripe` lib lazily, set api_key on first use.

    Raises RuntimeError with a clear message if the lib is missing or
    STRIPE_API_KEY is unset — callers (routes/webhook) translate this
    into a 503 so the API doesn't 500 in environments without billing.
    """
    global _stripe_module, _api_key_set
    if _stripe_module is None:
        try:
            import stripe  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised in CI w/o lib
            raise RuntimeError(
                "stripe package not installed. Add `stripe` to requirements.txt "
                "and rebuild the container."
            ) from exc
        _stripe_module = stripe
    if not _api_key_set:
        api_key = _env("STRIPE_API_KEY")
        if not api_key:
            raise RuntimeError(
                "STRIPE_API_KEY is unset. Billing endpoints are inert until "
                "configured (use sk_test_... in dev, or set STRIPE_TEST_MODE=1 "
                "+ STRIPE_TEST_API_KEY=sk_test_...)."
            )
        if _test_mode() and not api_key.startswith("sk_test_"):
            # Fail-SAFE: test mode must never fall back to a live key. If the
            # test key is missing/misset we'd otherwise resolve the live
            # STRIPE_API_KEY and "test" checkouts would charge real cards.
            raise RuntimeError(
                "STRIPE_TEST_MODE=1 but no sk_test_ key resolved. Set "
                "STRIPE_TEST_API_KEY=sk_test_... — refusing to use a live key "
                "in test mode."
            )
        if not api_key.startswith("sk_test_") and os.getenv("STRIPE_ALLOW_LIVE") != "1":
            # Belt-and-suspenders: refuse to use a live key unless an
            # explicit env flag is set. Aaron's hard constraint: live keys
            # are gated behind an explicit deploy step.
            raise RuntimeError(
                "Refusing to use a non-test Stripe key without STRIPE_ALLOW_LIVE=1. "
                "Live keys must be enabled by an explicit deploy step."
            )
        _stripe_module.api_key = api_key
        _api_key_set = True
    return _stripe_module


def is_configured() -> bool:
    """True iff a USABLE Stripe secret key is set for the active mode. Used by
    frontend feature gates / health checks. Does not actually load the SDK.

    Mirrors the _stripe() fail-safe: in test mode, a key that isn't sk_test_
    does NOT count as configured (a live-key fallback would be refused at load
    anyway), so a half-configured node reports billing-unavailable to the
    frontend instead of surfacing a flow that 503s at checkout."""
    key = _env("STRIPE_API_KEY")
    if not key:
        return False
    if _test_mode() and not key.startswith("sk_test_"):
        return False
    return True


# ---------------------------------------------------------------------------
# Price ID → tier mapping (env-driven).
# ---------------------------------------------------------------------------


def _legacy_pro_price_ids() -> tuple[str, ...]:
    """Legacy Stripe price IDs for pre-v3.23.0 Pro subscribers ($12/mo, $120/yr).

    Kept alive so existing subscribers whose Stripe subscription is tied
    to the old price IDs continue to resolve as `pro` in the webhook
    (Stripe charges them at the original $12 / $120 forever until cancel
    — there's no code-level grandfathering, just a "don't unmap these
    price IDs" promise). New signups go through STRIPE_PRO_PRICE_ID
    which the env points at the new $20 price.
    """
    return (
        "price_1TcbE0Dzk9HqAZnHO3cv6mWN",  # legacy Pro $12/mo
        "price_1TdcTfDzk9HqAZnHl979Vv0M",  # legacy Pro $120/yr
    )


def _legacy_suite_price_ids() -> tuple[str, ...]:
    """Legacy Suite price IDs (archived $25/mo). Kept for the same
    grandfather reason as legacy Pro."""
    return (
        "price_1TcbE1Dzk9HqAZnH9HVigcpm",  # legacy Suite $25/mo (archived)
    )


def price_id_to_tier(price_id: Optional[str]) -> str:
    """Map a Stripe price ID to a local tier name. Unknown / missing
    price IDs default to 'free' so a misconfigured webhook can't silently
    upgrade someone.

    Aaron's pricing decisions (locked 2026-05-29, v3.23.0 5-tier matrix
    landed 2026-06-02):
      Free        = $0,            browser-only
      Basic       = $7.99 / $79,   server text storage + sync + AI Chat + search (NO audio upload)
      Pro         = $20 / $200,    Meeting-Ops full server feature set incl. canonical_reprocess
      Suite       = $35 / $350,    Pro + uc_suite_entitlement (Pro on Project-Ops + Contact-Ops via Brigade fed.)
      Enterprise  = custom,        on-prem + BYOK + retention controls + HIPAA

    Legacy Pro price IDs ($12/mo + $120/yr) AND the legacy Suite $25 ID
    are intentionally kept in the mapping so pre-v3.23.0 subscribers stay
    grandfathered to their original price (Stripe keeps charging the old
    rate against the old price ID until the subscription is cancelled).
    """
    if not price_id:
        return "free"
    basic = _env("STRIPE_BASIC_PRICE_ID")
    basic_annual = _env("STRIPE_BASIC_ANNUAL_PRICE_ID")
    pro = _env("STRIPE_PRO_PRICE_ID")
    pro_annual = _env("STRIPE_PRO_ANNUAL_PRICE_ID")
    suite = _env("STRIPE_SUITE_PRICE_ID")
    suite_annual = _env("STRIPE_SUITE_ANNUAL_PRICE_ID")
    ent = _env("STRIPE_ENTERPRISE_PRICE_ID")
    if basic and price_id == basic:
        return "basic"
    if basic_annual and price_id == basic_annual:
        return "basic"
    if pro and price_id == pro:
        return "pro"
    if pro_annual and price_id == pro_annual:
        return "pro"
    if price_id in _legacy_pro_price_ids():
        # v3.23.0 grandfather: existing $12/mo + $120/yr subscribers keep
        # mapping to pro tier indefinitely (Stripe still charges their
        # original price; the local tier resolution shouldn't change).
        return "pro"
    if suite and price_id == suite:
        return "suite"
    if suite_annual and price_id == suite_annual:
        return "suite"
    if price_id in _legacy_suite_price_ids():
        return "suite"
    if ent and price_id == ent:
        return "enterprise"
    logger.warning(
        "Unknown Stripe price_id=%s; defaulting to 'free'. "
        "Set STRIPE_BASIC_PRICE_ID[_ANNUAL] / STRIPE_PRO_PRICE_ID[_ANNUAL] "
        "/ STRIPE_SUITE_PRICE_ID[_ANNUAL] / STRIPE_ENTERPRISE_PRICE_ID.",
        price_id,
    )
    return "free"


def price_id_is_basic(price_id: str) -> bool:
    """True iff the price ID matches a configured Basic-tier price (monthly
    or annual). Used by the webhook + Pricing UI flags."""
    if not price_id:
        return False
    monthly = _env("STRIPE_BASIC_PRICE_ID")
    annual = _env("STRIPE_BASIC_ANNUAL_PRICE_ID")
    return (bool(monthly) and price_id == monthly) or (
        bool(annual) and price_id == annual
    )


def price_id_is_pro(price_id: str) -> bool:
    """True iff the price ID matches a configured or legacy Pro-tier price.

    Includes the legacy $12 / $120 grandfathered IDs so an existing
    subscriber still flags as Pro for any code that asks."""
    if not price_id:
        return False
    monthly = _env("STRIPE_PRO_PRICE_ID")
    annual = _env("STRIPE_PRO_ANNUAL_PRICE_ID")
    if monthly and price_id == monthly:
        return True
    if annual and price_id == annual:
        return True
    return price_id in _legacy_pro_price_ids()


def price_id_is_suite(price_id: str) -> bool:
    """True iff the price ID matches the configured Suite tier price
    (monthly or annual), or the legacy $25 Suite ID.

    Used by the webhook handler — needed for the Project-Ops / Contact-Ops
    cross-app entitlement (`uc_suite_entitlement`) once Brigade federation
    deploys. Today the flag is documentation-only on the sibling apps."""
    if not price_id:
        return False
    monthly = _env("STRIPE_SUITE_PRICE_ID")
    annual = _env("STRIPE_SUITE_ANNUAL_PRICE_ID")
    if monthly and price_id == monthly:
        return True
    if annual and price_id == annual:
        return True
    return price_id in _legacy_suite_price_ids()


def tier_to_price_id(tier: str, billing_cycle: str = "monthly") -> Optional[str]:
    """Reverse of price_id_to_tier — used by /billing/checkout to translate
    the plan name from the request body into the Stripe price ID. Accepts
    "basic", "pro", "suite", and "enterprise" (enterprise returns None so
    /checkout can redirect to contact-sales).

    `billing_cycle`:
      - "monthly" (default): returns the monthly recurring price.
      - "annual":  returns the annual price ID for that tier. If the
                   annual env var is unset, falls back to monthly with a
                   warn log so a misconfigured node doesn't 400 the user.

    `billing_cycle="annual"` on plan="pro" is the trigger that drives the
    Founding 100 grant downstream in the Stripe webhook (the webhook
    detects `recurring.interval == "year"` AND that the price is the Pro
    annual price). The grant gate stays server-side; this function only
    chooses the price. Per Aaron 2026-06-02: Suite annual does NOT
    consume a Founding 100 seat — the cohort stays Pro-only.
    """
    tier = (tier or "").lower()
    cycle = (billing_cycle or "monthly").lower()

    if tier == "basic":
        if cycle == "annual":
            annual = _env("STRIPE_BASIC_ANNUAL_PRICE_ID")
            if annual:
                return annual
            logger.warning(
                "billing_cycle='annual' requested for plan='basic' but "
                "STRIPE_BASIC_ANNUAL_PRICE_ID is unset; falling back to monthly."
            )
        return _env("STRIPE_BASIC_PRICE_ID") or None

    if tier == "pro":
        if cycle == "annual":
            annual = _env("STRIPE_PRO_ANNUAL_PRICE_ID")
            if annual:
                return annual
            logger.warning(
                "billing_cycle='annual' requested for plan='pro' but "
                "STRIPE_PRO_ANNUAL_PRICE_ID is unset; falling back to monthly."
            )
        return _env("STRIPE_PRO_PRICE_ID") or None
    if tier == "pro_annual":
        # Convenience alias: someone passing plan="pro_annual" directly
        # gets the annual price without needing to also pass billing_cycle.
        annual = _env("STRIPE_PRO_ANNUAL_PRICE_ID")
        if annual:
            return annual
        logger.warning(
            "plan='pro_annual' requested but STRIPE_PRO_ANNUAL_PRICE_ID "
            "is unset; falling back to monthly pro price."
        )
        return _env("STRIPE_PRO_PRICE_ID") or None

    if tier == "suite":
        if cycle == "annual":
            annual = _env("STRIPE_SUITE_ANNUAL_PRICE_ID")
            if annual:
                return annual
            logger.warning(
                "billing_cycle='annual' requested for plan='suite' but "
                "STRIPE_SUITE_ANNUAL_PRICE_ID is unset; falling back to monthly."
            )
        return _env("STRIPE_SUITE_PRICE_ID") or None

    if tier == "enterprise":
        return _env("STRIPE_ENTERPRISE_PRICE_ID") or None
    return None


# ---------------------------------------------------------------------------
# Customer get-or-create
# ---------------------------------------------------------------------------


def get_or_create_stripe_customer(db: Session, user: User) -> str:
    """Ensure the user has a Stripe customer record + populated
    `stripe_customer_id`. Idempotent: re-using returns the existing ID.

    Returns the Stripe customer ID (cus_...).
    """
    if user.stripe_customer_id:
        return user.stripe_customer_id
    s = _stripe()
    customer = s.Customer.create(
        email=user.email,
        name=user.full_name or user.username,
        metadata={"meeting_ops_user_id": str(user.id)},
    )
    user.stripe_customer_id = customer.id
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("Created Stripe customer %s for user_id=%s", customer.id, user.id)
    return customer.id


# ---------------------------------------------------------------------------
# Checkout + Billing Portal
# ---------------------------------------------------------------------------


def create_checkout_session(
    db: Session,
    user: User,
    price_id: str,
    success_url: str,
    cancel_url: str,
    organization_id: Optional[int] = None,
) -> str:
    """Create a Stripe Checkout Session for a Subscription. Returns the
    hosted Checkout URL the frontend redirects to.

    `subscription_data.cancel_at_period_end` is NOT set here — that's a
    user-initiated action on the Billing Portal. Aaron's call: at-cancel
    we let the user finish the period rather than immediate prorate.
    The actual cancel-at-period-end UX lives in the Billing Portal.
    """
    s = _stripe()
    customer_id = get_or_create_stripe_customer(db, user)
    session = s.checkout.Session.create(
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        # Promotion codes are env-gated: STRIPE_ALLOW_PROMO_CODES=1 enables
        # the "Add promotion code" field on Stripe Checkout (used for
        # DEV_TEST_2026 testing + any future launch-day codes if we change
        # mind on the no-discount Founders 100 stance). Default OFF so
        # the production flow never accidentally surfaces a stale dev coupon.
        allow_promotion_codes=os.getenv("STRIPE_ALLOW_PROMO_CODES", "").strip() == "1",
        client_reference_id=str(user.id),
        # Subscription metadata travels through to the
        # customer.subscription.created webhook so we can audit the
        # provenance of every upgrade.
        subscription_data={
            "metadata": {
                "meeting_ops_user_id": str(user.id),
                # The org being upgraded. The webhook reads this back to set
                # Organization.plan so the per-org quota (monthly hours) actually
                # lifts on payment — user.tier alone doesn't move the quota.
                **(
                    {"meeting_ops_organization_id": str(organization_id)}
                    if organization_id is not None
                    else {}
                ),
                "source": "pricing_page",
            },
        },
    )
    return session.url


def create_billing_portal_session(user: User, return_url: str) -> str:
    """Create a Billing Portal session for an existing customer. Returns
    the hosted portal URL. The user must already have a Stripe customer
    record (raises ValueError otherwise — caller surfaces as 400)."""
    if not user.stripe_customer_id:
        raise ValueError("User has no Stripe customer record yet")
    s = _stripe()
    session = s.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=return_url,
    )
    return session.url


# ---------------------------------------------------------------------------
# Subscription lookup (for GET /api/billing/subscription)
# ---------------------------------------------------------------------------


def get_active_subscription(user: User) -> Optional[dict]:
    """Return the user's current active subscription as a small dict,
    or None if they don't have one. Used by /api/billing/subscription.
    """
    if not user.stripe_customer_id:
        return None
    s = _stripe()
    subs = s.Subscription.list(
        customer=user.stripe_customer_id,
        status="all",
        limit=10,
    )
    # Pick the first non-canceled subscription. Prefer active over
    # trialing/past_due so a renewed-after-cancel user shows the right
    # state.
    pick = None
    for sub in subs.auto_paging_iter() if hasattr(subs, "auto_paging_iter") else subs.data:
        if sub.status in ("active", "trialing", "past_due"):
            pick = sub
            break
        if sub.status in ("incomplete", "incomplete_expired") and pick is None:
            pick = sub
    if pick is None:
        return None
    price_id = None
    try:
        price_id = pick["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        pass
    return {
        "subscription_id": pick.id,
        "status": pick.status,
        "price_id": price_id,
        "tier": price_id_to_tier(price_id),
        "current_period_end": getattr(pick, "current_period_end", None),
        "cancel_at_period_end": bool(getattr(pick, "cancel_at_period_end", False)),
    }
