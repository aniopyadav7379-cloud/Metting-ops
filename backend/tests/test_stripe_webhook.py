"""Stripe webhook handler tests.

Covers the three things that must be true for the webhook to be safe:
  1. Bodies without a valid signature are rejected with 400.
  2. Duplicate event IDs are short-circuited (idempotency).
  3. Subscription created / updated / deleted map to the right
     User.tier transitions.

We don't go through the real Stripe SDK signature path (that requires
a real secret + a HMAC the SDK accepts). Instead we monkeypatch
`services.stripe_client._stripe` to return a stub whose
`Webhook.construct_event` returns the dict we feed it, and we exercise
both happy paths and the "no signature" error path explicitly.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

def _models():
    """Import inside fn so conftest's app fixture has reloaded modules first."""
    from auth.models import User
    from database.database import SessionLocal
    return User, SessionLocal


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _stripe_env(monkeypatch):
    """Provide the minimum env vars so the webhook doesn't 503 on config."""
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_dummy")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_dummy")
    monkeypatch.setenv("STRIPE_PRO_PRICE_ID", "price_pro_test")
    monkeypatch.setenv("STRIPE_PRO_ANNUAL_PRICE_ID", "price_pro_annual_test")
    monkeypatch.setenv("STRIPE_BASIC_PRICE_ID", "price_basic_test")
    monkeypatch.setenv("STRIPE_BASIC_ANNUAL_PRICE_ID", "price_basic_annual_test")
    monkeypatch.setenv("STRIPE_SUITE_PRICE_ID", "price_suite_test")
    monkeypatch.setenv("STRIPE_SUITE_ANNUAL_PRICE_ID", "price_suite_annual_test")
    monkeypatch.setenv("STRIPE_ENTERPRISE_PRICE_ID", "price_ent_test")
    monkeypatch.setenv("FOUNDING_100_ACTIVE", "true")
    monkeypatch.setenv("FOUNDING_100_LIMIT", "100")
    monkeypatch.delenv("FOUNDERS_100_ACTIVE", raising=False)
    monkeypatch.delenv("FOUNDERS_100_LIMIT", raising=False)
    # Make absolutely sure no test bleeds real Redis state — leave REDIS_URL
    # alone; _redis_client returns None on import failure / unset URL,
    # which falls through to "always process".
    monkeypatch.delenv("REDIS_URL", raising=False)
    yield


class _StubStripeWebhook:
    """Stand-in for `stripe.Webhook` that hands us back the event dict."""

    def __init__(self, event):
        self._event = event

    def construct_event(self, payload, sig_header, secret):
        if sig_header == "BAD":
            raise ValueError("Invalid signature")
        return self._event


class _StubStripeModule:
    def __init__(self, event):
        self.Webhook = _StubStripeWebhook(event)
        self.api_key = None


def _patch_stripe(monkeypatch, event):
    """Patch the `_stripe()` helper to return a stub module that yields
    the given event from `construct_event`."""
    stub = _StubStripeModule(event)
    monkeypatch.setattr(
        "services.stripe_client._stripe",
        lambda: stub,
    )
    monkeypatch.setattr(
        "api.stripe_webhook._stripe",
        lambda: stub,
    )
    return stub


def _make_user(email=None, *, customer_id=None, tier="free"):
    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        uname = "wh" + uuid.uuid4().hex[:8]
        user = User(
            email=email or f"{uname}@example.com",
            username=uname,
            hashed_password="x",
            is_active=True,
            is_verified=True,
            tier=tier,
            stripe_customer_id=customer_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def _refresh(user):
    User, SessionLocal = _models()
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user.id).first()
    finally:
        db.close()


def _make_org(*, plan="free", max_monthly_hours=10):
    """Create an org with a stale per-org hours override so a test can assert
    the override gets CLEARED on upgrade (the v3.32.0 quota lift)."""
    from auth.models import Organization
    from database.database import SessionLocal
    db = SessionLocal()
    try:
        slug = "whorg" + uuid.uuid4().hex[:8]
        org = Organization(
            name=f"WH Org {slug}",
            slug=slug,
            plan=plan,
            max_monthly_hours=max_monthly_hours,
        )
        db.add(org)
        db.commit()
        db.refresh(org)
        return org
    finally:
        db.close()


def _refresh_org(org):
    from auth.models import Organization
    from database.database import SessionLocal
    db = SessionLocal()
    try:
        return db.query(Organization).filter(Organization.id == org.id).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. Signature verification
# ---------------------------------------------------------------------------


def test_webhook_rejects_missing_signature(client, monkeypatch):
    _patch_stripe(monkeypatch, {"id": "evt_x", "type": "customer.created"})
    r = client.post("/api/stripe/webhook", content=b"{}")
    assert r.status_code == 400
    assert "stripe-signature" in r.json()["detail"].lower()


def test_webhook_rejects_invalid_signature(client, monkeypatch):
    _patch_stripe(monkeypatch, {"id": "evt_x", "type": "customer.created"})
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "BAD"},
    )
    assert r.status_code == 400


def test_webhook_503s_when_secret_missing(client, monkeypatch):
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    _patch_stripe(monkeypatch, {"id": "evt_x", "type": "customer.created"})
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# 2. Idempotency
# ---------------------------------------------------------------------------


def test_webhook_idempotency_short_circuits_duplicate(client, monkeypatch, app):
    """Two deliveries of the same event ID. First processes, second is
    short-circuited.

    Redis is not available in tests, so this exercises the "no Redis"
    fallback path: both calls return 200, and there is no double-write
    visible side-effect since both handlers are idempotent themselves.
    To exercise the *real* Redis short-circuit we patch _redis_client to
    return an in-memory dict-backed stub.
    """
    user = _make_user(customer_id="cus_idem")

    # Replace the redis client with a tiny in-memory stub so we can prove
    # the second call really did skip processing.
    seen: dict[str, str] = {}

    class _Fake:
        def set(self, key, value, nx=False, ex=None):
            if nx and key in seen:
                return False
            seen[key] = value
            return True

    monkeypatch.setattr("api.stripe_webhook._redis_client", lambda: _Fake())

    event = {
        "id": "evt_dup_1",
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_1",
                "status": "active",
                "customer": "cus_idem",
                "items": {
                    "data": [{"price": {"id": "price_pro_test"}}]
                },
            }
        },
    }
    _patch_stripe(monkeypatch, event)

    r1 = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    r2 = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r1.status_code == 200
    assert r1.json().get("handled") is True
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True

    # User was upgraded exactly once.
    refreshed = _refresh(user)
    assert refreshed.tier == "pro"


# ---------------------------------------------------------------------------
# 3. Event → tier mapping
# ---------------------------------------------------------------------------


def test_subscription_created_pro_price_flips_to_pro(client, monkeypatch):
    user = _make_user(customer_id="cus_created_pro", tier="free")
    event = {
        "id": "evt_created_pro_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_pro_1",
                "status": "active",
                "customer": "cus_created_pro",
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "pro"


def test_subscription_created_pro_lifts_org_plan_and_clears_quota_override(client, monkeypatch):
    """The v3.32.0 quota lift: a Pro sub carrying meeting_ops_organization_id must
    flip Organization.plan to 'pro' AND clear the stale max_monthly_hours override,
    so services.quotas falls through to the 100h Pro default (10h -> 100h). The
    other webhook tests only assert User.tier; this guards the actual quota lift
    from silently regressing to the old tier-but-not-quota bug."""
    user = _make_user(customer_id="cus_org_pro", tier="free")
    org = _make_org(plan="free", max_monthly_hours=10)
    event = {
        "id": "evt_org_pro_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_org_pro_1",
                "status": "active",
                "customer": "cus_org_pro",
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
                "metadata": {
                    "meeting_ops_user_id": str(user.id),
                    "meeting_ops_organization_id": str(org.id),
                },
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "pro"
    refreshed = _refresh_org(org)
    assert refreshed.plan == "pro"           # quota plan follows the payment
    assert refreshed.max_monthly_hours is None  # stale 10h override cleared -> 100h default


def test_subscription_created_incomplete_does_not_upgrade(client, monkeypatch):
    user = _make_user(customer_id="cus_incomplete", tier="free")
    event = {
        "id": "evt_incomplete_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_inc_1",
                "status": "incomplete",
                "customer": "cus_incomplete",
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "free"


def test_subscription_updated_canceled_flips_to_free(client, monkeypatch):
    user = _make_user(customer_id="cus_cancelled", tier="pro")
    event = {
        "id": "evt_cancel_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_1",
                "status": "canceled",
                "customer": "cus_cancelled",
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "free"


def test_subscription_updated_past_due_keeps_paid_tier(client, monkeypatch):
    user = _make_user(customer_id="cus_past_due", tier="pro")
    event = {
        "id": "evt_past_due_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_2",
                "status": "past_due",
                "customer": "cus_past_due",
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    # Grace period — keep them on pro while Stripe retries the card.
    assert _refresh(user).tier == "pro"


def test_subscription_deleted_flips_to_free(client, monkeypatch):
    user = _make_user(customer_id="cus_deleted", tier="pro")
    event = {
        "id": "evt_del_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_3",
                "status": "canceled",
                "customer": "cus_deleted",
                "items": {"data": [{"price": {"id": "price_pro_test"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "free"


def test_customer_created_links_customer_id(client, monkeypatch):
    user = _make_user(email="cust@example.com", customer_id=None)
    event = {
        "id": "evt_cust_" + uuid.uuid4().hex[:6],
        "type": "customer.created",
        "data": {
            "object": {
                "id": "cus_link_test",
                "email": "cust@example.com",
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).stripe_customer_id == "cus_link_test"


def test_unhandled_event_type_is_200_not_500(client, monkeypatch):
    event = {
        "id": "evt_unknown_" + uuid.uuid4().hex[:6],
        "type": "checkout.session.async_payment_failed",
        "data": {"object": {}},
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert r.json().get("handled") is False


def test_subscription_created_basic_price_flips_to_basic(client, monkeypatch):
    """v3.23.0: Basic price ID flips user to basic tier."""
    user = _make_user(customer_id="cus_basic_new", tier="free")
    event = {
        "id": "evt_basic_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_basic_1",
                "status": "active",
                "customer": "cus_basic_new",
                "items": {"data": [{"price": {"id": "price_basic_test"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "basic"


def test_subscription_created_basic_annual_flips_to_basic(client, monkeypatch):
    user = _make_user(customer_id="cus_basic_yr", tier="free")
    event = {
        "id": "evt_basic_yr_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_basic_yr_1",
                "status": "active",
                "customer": "cus_basic_yr",
                "items": {"data": [{"price": {"id": "price_basic_annual_test"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "basic"


def test_subscription_created_suite_price_flips_to_suite(client, monkeypatch):
    """v3.23.0: Suite price ID flips user to suite tier (not pro)."""
    user = _make_user(customer_id="cus_suite_new", tier="free")
    event = {
        "id": "evt_suite_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_suite_1",
                "status": "active",
                "customer": "cus_suite_new",
                "items": {"data": [{"price": {"id": "price_suite_test"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "suite"


def test_subscription_created_legacy_pro_12_grandfathered_to_pro(client, monkeypatch):
    """v3.23.0 grandfather: pre-reprice $12 Pro price ID still resolves to pro tier."""
    user = _make_user(customer_id="cus_legacy_pro", tier="free")
    event = {
        "id": "evt_legacy_pro_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_legacy",
                "status": "active",
                "customer": "cus_legacy_pro",
                "items": {"data": [{"price": {
                    "id": "price_1TcbE0Dzk9HqAZnHO3cv6mWN",  # legacy $12/mo
                }}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    assert _refresh(user).tier == "pro"


def test_founding_100_not_granted_on_suite_annual(client, monkeypatch):
    """v3.23.0 (Aaron 2026-06-02): Suite annual checkouts pay $350/yr
    annual-upfront, but do NOT consume a Founding 100 seat. The cohort
    stays Pro-only — a future Suite-cohort can be opened separately."""
    user = _make_user(customer_id="cus_suite_annual", tier="free")
    event = {
        "id": "evt_suite_yr_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_suite_yr",
                "status": "active",
                "customer": "cus_suite_annual",
                "items": {"data": [{"price": {
                    "id": "price_suite_annual_test",
                    "recurring": {"interval": "year"},
                }}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    refreshed = _refresh(user)
    assert refreshed.tier == "suite"
    # Founding 100 grant is Pro-only — Suite annual does not qualify.
    assert refreshed.is_founding_member is False


def test_founding_100_granted_on_pro_annual(client, monkeypatch):
    """Sanity-check the inverse: Pro annual still triggers the Founding 100 grant."""
    user = _make_user(customer_id="cus_pro_annual_f100", tier="free")
    event = {
        "id": "evt_pro_yr_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_pro_yr_f100",
                "status": "active",
                "customer": "cus_pro_annual_f100",
                "items": {"data": [{"price": {
                    "id": "price_pro_annual_test",
                    "recurring": {"interval": "year"},
                }}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    refreshed = _refresh(user)
    assert refreshed.tier == "pro"
    assert refreshed.is_founding_member is True


def test_subscription_unknown_price_id_does_not_upgrade(client, monkeypatch):
    user = _make_user(customer_id="cus_weird_price", tier="free")
    event = {
        "id": "evt_weird_" + uuid.uuid4().hex[:6],
        "type": "customer.subscription.created",
        "data": {
            "object": {
                "id": "sub_weird",
                "status": "active",
                "customer": "cus_weird_price",
                "items": {"data": [{"price": {"id": "price_unknown"}}]},
            }
        },
    }
    _patch_stripe(monkeypatch, event)
    r = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "ok"},
    )
    assert r.status_code == 200
    # Unknown price defaults to "free" — no silent upgrade.
    assert _refresh(user).tier == "free"
