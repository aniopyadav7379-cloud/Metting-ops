"""Regression tests for per-org upload quotas (services/quotas.py).

Guards the v3.47.x bug where paid tiers missing from TIER_DEFAULTS
(basic/sync/suite) silently fell through to FREE limits — a $35 Suite org
ended up *more* upload-restricted than a $20 Pro.
"""
from __future__ import annotations

from auth.models import Organization
from services.quotas import TIER_DEFAULTS, get_org_limits

PAID_TIERS = ("sync", "basic", "pro", "suite", "enterprise")


def test_every_paid_tier_has_explicit_defaults():
    for tier in PAID_TIERS:
        assert tier in TIER_DEFAULTS, f"{tier!r} missing from TIER_DEFAULTS -> falls through to free"


def test_suite_is_at_least_pro():
    pro, suite = TIER_DEFAULTS["pro"], TIER_DEFAULTS["suite"]
    assert suite["max_file_bytes"] >= pro["max_file_bytes"]
    assert suite["max_concurrent_uploads"] >= pro["max_concurrent_uploads"]
    assert suite["max_monthly_hours"] is None or suite["max_monthly_hours"] >= pro["max_monthly_hours"]


def test_basic_is_alias_of_sync():
    assert TIER_DEFAULTS["basic"] == TIER_DEFAULTS["sync"]


def test_get_org_limits_suite_not_free():
    """A suite org resolves to SUITE limits, not FREE (the bug)."""
    org = Organization(name="q-suite", slug="q-suite", plan="suite")
    lim = get_org_limits(org)
    assert lim["plan"] == "suite"
    assert lim["max_file_bytes"] == TIER_DEFAULTS["suite"]["max_file_bytes"]
    assert lim["max_monthly_hours"] == TIER_DEFAULTS["suite"]["max_monthly_hours"]
    assert lim["max_monthly_hours"] != TIER_DEFAULTS["free"]["max_monthly_hours"]


def test_get_org_limits_basic_not_free():
    org = Organization(name="q-basic", slug="q-basic", plan="basic")
    lim = get_org_limits(org)
    assert lim["max_file_bytes"] == TIER_DEFAULTS["sync"]["max_file_bytes"]


def test_per_org_override_still_wins():
    org = Organization(name="q-ovr", slug="q-ovr", plan="pro", max_monthly_hours=500)
    lim = get_org_limits(org)
    assert lim["max_monthly_hours"] == 500


def test_unknown_plan_falls_back_to_free():
    org = Organization(name="q-unk", slug="q-unk", plan="nonexistent")
    lim = get_org_limits(org)
    assert lim["max_file_bytes"] == TIER_DEFAULTS["free"]["max_file_bytes"]
