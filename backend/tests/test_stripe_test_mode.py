"""Tests for the Stripe TEST-mode switch (services/stripe_client.py).

STRIPE_TEST_MODE=1 flips every STRIPE_<X> config read onto its
STRIPE_TEST_<X> variant — api key, webhook secret, publishable key, all
price IDs — without touching the live vars. These tests pin:

  - the flag parsing (1/true/yes/on, case-insensitive),
  - the redirection (test var wins; falls back to the plain var if unset),
  - the live-mode behavior is unchanged (plain vars only),
  - and the FAIL-SAFE: test mode refuses to load a non-sk_test_ key, so a
    half-configured node goes inert instead of charging a real card.
"""

from __future__ import annotations

import types

import pytest

from services import stripe_client


# All the STRIPE_* vars that could leak across a test; clear them so each
# test starts from a known-empty environment.
_ALL = [
    "STRIPE_TEST_MODE",
    "STRIPE_API_KEY", "STRIPE_TEST_API_KEY",
    "STRIPE_WEBHOOK_SECRET", "STRIPE_TEST_WEBHOOK_SECRET",
    "STRIPE_PUBLISHABLE_KEY", "STRIPE_TEST_PUBLISHABLE_KEY",
    "STRIPE_PRO_PRICE_ID", "STRIPE_TEST_PRO_PRICE_ID",
    "STRIPE_ALLOW_LIVE",
]


@pytest.fixture
def clean_env(monkeypatch):
    for k in _ALL:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# --- flag parsing -----------------------------------------------------------

@pytest.mark.parametrize("val,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("Yes", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("nope", False),
])
def test_test_mode_flag_parsing(clean_env, val, expected):
    clean_env.setenv("STRIPE_TEST_MODE", val)
    assert stripe_client.test_mode() is expected


def test_test_mode_unset_is_false(clean_env):
    assert stripe_client.test_mode() is False


# --- redirection ------------------------------------------------------------

def test_env_redirects_to_test_var_when_test_mode_on(clean_env):
    clean_env.setenv("STRIPE_TEST_MODE", "1")
    clean_env.setenv("STRIPE_API_KEY", "sk_live_REAL")
    clean_env.setenv("STRIPE_TEST_API_KEY", "sk_test_DUMMY")
    assert stripe_client._env("STRIPE_API_KEY") == "sk_test_DUMMY"


def test_env_falls_back_to_plain_when_test_var_unset(clean_env):
    # Test mode on, but no STRIPE_TEST_PUBLISHABLE_KEY set → plain var used.
    clean_env.setenv("STRIPE_TEST_MODE", "1")
    clean_env.setenv("STRIPE_PUBLISHABLE_KEY", "pk_live_REAL")
    assert stripe_client._env("STRIPE_PUBLISHABLE_KEY") == "pk_live_REAL"


def test_env_uses_plain_var_when_test_mode_off(clean_env):
    # Live mode: the test var is ignored even when present.
    clean_env.setenv("STRIPE_API_KEY", "sk_live_REAL")
    clean_env.setenv("STRIPE_TEST_API_KEY", "sk_test_DUMMY")
    assert stripe_client._env("STRIPE_API_KEY") == "sk_live_REAL"


def test_price_id_and_webhook_follow_the_switch(clean_env):
    clean_env.setenv("STRIPE_TEST_MODE", "1")
    clean_env.setenv("STRIPE_PRO_PRICE_ID", "price_LIVE_pro")
    clean_env.setenv("STRIPE_TEST_PRO_PRICE_ID", "price_TEST_pro")
    clean_env.setenv("STRIPE_WEBHOOK_SECRET", "whsec_LIVE")
    clean_env.setenv("STRIPE_TEST_WEBHOOK_SECRET", "whsec_TEST")
    assert stripe_client.tier_to_price_id("pro") == "price_TEST_pro"
    assert stripe_client.price_id_to_tier("price_TEST_pro") == "pro"
    # The LIVE price id must NOT resolve while in test mode (separate worlds).
    assert stripe_client.price_id_to_tier("price_LIVE_pro") == "free"
    assert stripe_client.webhook_secret() == "whsec_TEST"


def test_is_configured_reflects_active_mode(clean_env):
    # Test mode on but no test key → not configured (even if a live key exists).
    clean_env.setenv("STRIPE_TEST_MODE", "1")
    clean_env.setenv("STRIPE_API_KEY", "sk_live_REAL")
    assert stripe_client.is_configured() is False
    clean_env.setenv("STRIPE_TEST_API_KEY", "sk_test_DUMMY")
    assert stripe_client.is_configured() is True


# --- the fail-safe ----------------------------------------------------------

def _reset_stripe_cache(monkeypatch):
    """_stripe() caches the module + api_key after first load. Reset both so
    a test exercises the key-validation branch fresh, and inject a fake
    `stripe` module so no real import/network happens."""
    fake = types.SimpleNamespace(api_key=None)
    monkeypatch.setattr(stripe_client, "_stripe_module", fake, raising=False)
    monkeypatch.setattr(stripe_client, "_api_key_set", False, raising=False)
    return fake


def test_stripe_refuses_live_key_in_test_mode(clean_env):
    """STRIPE_TEST_MODE=1 with no sk_test_ resolved must raise, NOT silently
    fall back to the live key (which would charge real cards)."""
    _reset_stripe_cache(clean_env)
    clean_env.setenv("STRIPE_TEST_MODE", "1")
    clean_env.setenv("STRIPE_API_KEY", "sk_live_REAL")  # no STRIPE_TEST_API_KEY
    with pytest.raises(RuntimeError, match="refusing to use a live key"):
        stripe_client._stripe()


def test_stripe_loads_test_key_in_test_mode(clean_env):
    _reset_stripe_cache(clean_env)
    clean_env.setenv("STRIPE_TEST_MODE", "1")
    clean_env.setenv("STRIPE_API_KEY", "sk_live_REAL")
    clean_env.setenv("STRIPE_TEST_API_KEY", "sk_test_DUMMY")
    s = stripe_client._stripe()
    assert s.api_key == "sk_test_DUMMY"


def test_stripe_live_key_still_requires_allow_live(clean_env):
    """Live-mode guard is unchanged: a non-test key needs STRIPE_ALLOW_LIVE=1."""
    _reset_stripe_cache(clean_env)
    clean_env.setenv("STRIPE_API_KEY", "sk_live_REAL")  # test mode off
    with pytest.raises(RuntimeError, match="STRIPE_ALLOW_LIVE"):
        stripe_client._stripe()
    _reset_stripe_cache(clean_env)
    clean_env.setenv("STRIPE_ALLOW_LIVE", "1")
    s = stripe_client._stripe()
    assert s.api_key == "sk_live_REAL"
