#!/usr/bin/env python3
"""Provision Stripe TEST-mode products / prices / webhook for Meeting-Ops.

Given a Stripe TEST secret key (sk_test_...), this:
  1. Creates (idempotently, keyed on Stripe `lookup_key`) the test-mode
     recurring prices for Basic / Pro / Suite, monthly + annual, at Aaron's
     locked amounts (Basic $7.99/$79, Pro $20/$200, Suite $35/$350).
  2. Creates (or, with --recreate-webhook, replaces) a webhook endpoint
     pointing at the instance's /api/stripe/webhook with the events the
     handler dispatches on.
  3. Prints the STRIPE_TEST_* env block to paste into deploy/bigboy/.env.bigboy.

It does NOT touch live data: it refuses to run with anything but an sk_test_
key. Re-running is safe — existing prices are reused by lookup_key, so you
get the same price IDs every time.

The webhook SIGNING SECRET (whsec_...) is only returned by Stripe at
endpoint-CREATION time. If the endpoint already exists this script can't read
it back — pass --recreate-webhook to delete + recreate and get a fresh secret,
or grab it from the Dashboard (Developers -> Webhooks -> the endpoint).

Usage:
    # key via env (preferred — keeps it out of shell history / argv):
    STRIPE_TEST_API_KEY=sk_test_xxx python3 stripe_test_provision.py

    # or explicitly:
    python3 stripe_test_provision.py --key sk_test_xxx \
        --webhook-url https://meetingops.magicunicorn.dev/api/stripe/webhook \
        --recreate-webhook

Run it inside the backend container (stripe is installed there):
    docker exec -i -e STRIPE_TEST_API_KEY=sk_test_xxx meet-backend \
        python3 /app/scripts/stripe_test_provision.py --recreate-webhook
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_WEBHOOK_URL = "https://meetingops.magicunicorn.dev/api/stripe/webhook"

# The events stripe_webhook.py dispatches on (api/stripe_webhook.py _DISPATCH).
WEBHOOK_EVENTS = [
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.created",
    "checkout.session.completed",
]

# Aaron's locked v3.23.0 pricing (unit_amount is in cents). Each entry maps a
# STRIPE_TEST_* env var name to the price spec. `lookup_key` is unique per
# Stripe account, which is what makes this script idempotent.
PRICE_PLAN = [
    # env var,                            tier,    label,                interval, cents, lookup_key
    ("STRIPE_TEST_BASIC_PRICE_ID",        "basic", "Meeting-Ops Basic",  "month",   799,  "mo_test_basic_monthly"),
    ("STRIPE_TEST_BASIC_ANNUAL_PRICE_ID", "basic", "Meeting-Ops Basic",  "year",   7900,  "mo_test_basic_annual"),
    ("STRIPE_TEST_PRO_PRICE_ID",          "pro",   "Meeting-Ops Pro",    "month",  2000,  "mo_test_pro_monthly"),
    ("STRIPE_TEST_PRO_ANNUAL_PRICE_ID",   "pro",   "Meeting-Ops Pro",    "year",  20000,  "mo_test_pro_annual"),
    ("STRIPE_TEST_SUITE_PRICE_ID",        "suite", "Meeting-Ops Suite",  "month",  3500,  "mo_test_suite_monthly"),
    ("STRIPE_TEST_SUITE_ANNUAL_PRICE_ID", "suite", "Meeting-Ops Suite",  "year",  35000,  "mo_test_suite_annual"),
]


def _eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


def get_or_create_price(stripe, *, lookup_key, product_name, tier, interval, cents):
    """Reuse the price with this lookup_key if it exists, else create the
    product + price. Returns the price id."""
    existing = stripe.Price.list(lookup_keys=[lookup_key], active=True, limit=1)
    if existing.data:
        pid = existing.data[0].id
        _eprint(f"  reuse  {lookup_key:24s} -> {pid}")
        return pid
    product = stripe.Product.create(
        name=product_name,
        metadata={"meeting_ops_tier": tier, "meeting_ops_env": "test"},
    )
    price = stripe.Price.create(
        product=product.id,
        unit_amount=cents,
        currency="usd",
        recurring={"interval": interval},
        lookup_key=lookup_key,
        metadata={
            "meeting_ops_tier": tier,
            "meeting_ops_env": "test",
            "billing_cycle": "monthly" if interval == "month" else "annual",
        },
    )
    _eprint(f"  create {lookup_key:24s} -> {price.id}  (${cents/100:.2f}/{interval})")
    return price.id


def get_or_create_webhook(stripe, url, *, recreate):
    """Find the webhook endpoint for `url`. With recreate=True, delete and
    re-make it to obtain a fresh signing secret. Returns (endpoint, secret)
    where secret is None if we reused a pre-existing endpoint (Stripe only
    reveals the secret at creation)."""
    found = None
    for ep in stripe.WebhookEndpoint.list(limit=100).auto_paging_iter():
        if ep.url == url:
            found = ep
            break
    if found and recreate:
        _eprint(f"  delete existing webhook {found.id} (recreate)")
        stripe.WebhookEndpoint.delete(found.id)
        found = None
    if found:
        _eprint(f"  reuse  webhook {found.id} (secret NOT retrievable — use --recreate-webhook or the Dashboard)")
        return found, None
    ep = stripe.WebhookEndpoint.create(
        url=url,
        enabled_events=WEBHOOK_EVENTS,
        description="Meeting-Ops test-mode webhook (auto-provisioned)",
    )
    _eprint(f"  create webhook {ep.id} -> secret captured")
    return ep, ep.secret


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--key", default=os.getenv("STRIPE_TEST_API_KEY", ""),
                    help="Stripe TEST secret key (sk_test_...); or set STRIPE_TEST_API_KEY")
    ap.add_argument("--publishable", default=os.getenv("STRIPE_TEST_PUBLISHABLE_KEY", ""),
                    help="Stripe TEST publishable key (pk_test_...); optional, echoed into the env block")
    ap.add_argument("--webhook-url", default=DEFAULT_WEBHOOK_URL)
    ap.add_argument("--recreate-webhook", action="store_true",
                    help="Delete + recreate the webhook endpoint to obtain a fresh signing secret")
    args = ap.parse_args()

    key = (args.key or "").strip()
    if not key:
        _eprint("ERROR: no key. Pass --key sk_test_... or set STRIPE_TEST_API_KEY.")
        return 2
    if not key.startswith("sk_test_"):
        _eprint(f"ERROR: refusing to run with a non-test key (got {key[:8]}...). "
                "This script only provisions TEST data.")
        return 2

    try:
        import stripe  # type: ignore
    except ImportError:
        _eprint("ERROR: the `stripe` package isn't installed here. Run inside the "
                "backend container: docker exec -i -e STRIPE_TEST_API_KEY=... "
                "meet-backend python3 /app/scripts/stripe_test_provision.py")
        return 2
    stripe.api_key = key

    _eprint("Provisioning Stripe TEST prices ...")
    resolved: dict[str, str] = {}
    for env_var, tier, label, interval, cents, lookup_key in PRICE_PLAN:
        resolved[env_var] = get_or_create_price(
            stripe, lookup_key=lookup_key, product_name=label,
            tier=tier, interval=interval, cents=cents,
        )

    _eprint("Provisioning webhook endpoint ...")
    ep, secret = get_or_create_webhook(stripe, args.webhook_url, recreate=args.recreate_webhook)

    # --- emit the env block on stdout (stderr above is just progress) -------
    print("\n# ---- paste into deploy/bigboy/.env.bigboy (then deploy backend) ----")
    print("STRIPE_TEST_MODE=1")
    print(f"STRIPE_TEST_API_KEY={key}")
    if args.publishable:
        print(f"STRIPE_TEST_PUBLISHABLE_KEY={args.publishable}")
    else:
        print("STRIPE_TEST_PUBLISHABLE_KEY=   # pk_test_... (optional; not needed for redirect Checkout)")
    if secret:
        print(f"STRIPE_TEST_WEBHOOK_SECRET={secret}")
    else:
        print(f"STRIPE_TEST_WEBHOOK_SECRET=   # whsec_... — endpoint {ep.id} already existed; "
              "re-run with --recreate-webhook or copy from the Stripe Dashboard")
    for env_var, *_ in PRICE_PLAN:
        print(f"{env_var}={resolved[env_var]}")
    print(f"# webhook endpoint: {ep.url}  ({ep.id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
