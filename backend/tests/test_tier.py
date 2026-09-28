"""Tests for tier-based feature gating (backend/auth/tier.py).

Covers:
  - TIER_FEATURES dict shape (all tiers have the same keys)
  - get_user_tier resolves the column value
  - is_superuser overrides to 'enterprise'
  - get_tier_features returns per-tier dicts
  - require_tier raises 403 below min, passes through at or above min
  - require_tier rejects unknown min_tier at definition time
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import pytest
from fastapi import HTTPException

from auth.tier import (
    TIER_FEATURES,
    get_tier_features,
    get_user_tier,
    require_tier,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@dataclass
class FakeUser:
    """Stand-in for `auth.models.User` so we don't need a DB session."""

    tier: Optional[str] = "free"
    is_superuser: bool = False


# ---------------------------------------------------------------------------
# TIER_FEATURES shape
# ---------------------------------------------------------------------------


def test_tier_features_has_all_five_tiers():
    """The five tier names must be present and only those five
    (v3.23.0 added basic + suite)."""
    assert set(TIER_FEATURES.keys()) == {
        "free",
        "basic",
        "pro",
        "suite",
        "enterprise",
    }


def test_tier_features_have_consistent_keys():
    """Every tier dict has the same capability keys, so frontend code
    that iterates capabilities never crashes on missing keys.

    v3.23.0 added: audio_upload, server_text_storage, ai_chat_over_corpus,
    cross_meeting_search, uc_suite_entitlement, byok_models, live_diarization.
    """
    expected_keys = {
        "server_live",
        "diarization",
        "live_diarization",
        "qwen36_summary",
        "speaker_library",
        "retention_controls",
        "brigade_byok",
        "hipaa",
        "canonical_reprocess",
        "brigade_integration",
        "cross_device_sync",
        "bulk_import",
        "agent_write_basic",
        "agent_write_reprocess",
        "agent_write_email_draft",
        "audio_upload",
        "server_text_storage",
        "ai_chat_over_corpus",
        "cross_meeting_search",
        "uc_suite_entitlement",
        "byok_models",
        "max_sessions_per_month",
        "max_session_minutes",
    }
    for tier_name, features in TIER_FEATURES.items():
        assert set(features.keys()) == expected_keys, (
            f"tier {tier_name!r} has wrong keys: "
            f"{set(features.keys()) ^ expected_keys}"
        )


def test_tier_features_capabilities_escalate():
    """free should have fewer enabled booleans than basic, which has
    fewer than pro, which has fewer than suite, which has fewer than
    enterprise. (Suite == Pro on the Meeting-Ops surface but adds
    uc_suite_entitlement, so still > Pro by 1.)"""
    bool_keys = [
        "server_live",
        "diarization",
        "qwen36_summary",
        "speaker_library",
        "retention_controls",
        "brigade_byok",
        "hipaa",
        "canonical_reprocess",
        "brigade_integration",
        "cross_device_sync",
        "bulk_import",
        "audio_upload",
        "server_text_storage",
        "ai_chat_over_corpus",
        "cross_meeting_search",
        "uc_suite_entitlement",
        "byok_models",
    ]
    free_enabled = sum(1 for k in bool_keys if TIER_FEATURES["free"][k])
    basic_enabled = sum(1 for k in bool_keys if TIER_FEATURES["basic"][k])
    pro_enabled = sum(1 for k in bool_keys if TIER_FEATURES["pro"][k])
    suite_enabled = sum(1 for k in bool_keys if TIER_FEATURES["suite"][k])
    ent_enabled = sum(1 for k in bool_keys if TIER_FEATURES["enterprise"][k])

    assert free_enabled < basic_enabled < pro_enabled < suite_enabled < ent_enabled


def test_tier_features_quotas_unlimited_above_free():
    """basic / pro / suite / enterprise should all have None (unlimited)
    quotas, free has real numbers."""
    assert TIER_FEATURES["free"]["max_sessions_per_month"] == 50
    assert TIER_FEATURES["free"]["max_session_minutes"] == 60
    for paid in ("basic", "pro", "suite", "enterprise"):
        assert TIER_FEATURES[paid]["max_sessions_per_month"] is None
        assert TIER_FEATURES[paid]["max_session_minutes"] is None


def test_basic_tier_text_vs_audio_split():
    """The defining v3.23.0 architectural rule: Basic gets server-side
    TEXT processing (storage / search / chat-over-corpus). Pro gets
    server-side AUDIO processing (canonical_reprocess / diarization /
    speaker_library). This test pins the split so a future flag-juggle
    doesn't accidentally let Basic upload audio."""
    basic = TIER_FEATURES["basic"]
    # Server text gates ON for Basic.
    assert basic["server_text_storage"] is True
    assert basic["ai_chat_over_corpus"] is True
    assert basic["cross_meeting_search"] is True
    assert basic["cross_device_sync"] is True
    # Server audio gates OFF for Basic.
    assert basic["audio_upload"] is False
    assert basic["canonical_reprocess"] is False
    assert basic["qwen36_summary"] is False
    assert basic["speaker_library"] is False
    assert basic["brigade_integration"] is False
    assert basic["bulk_import"] is False
    # uc_suite_entitlement is Suite/Enterprise-only.
    assert basic["uc_suite_entitlement"] is False
    # BYOK is enterprise-only.
    assert basic["byok_models"] is False


def test_suite_tier_carries_uc_suite_entitlement():
    """Suite is Pro + uc_suite_entitlement on the Meeting-Ops surface.
    The cross-app benefit is the Suite-vs-Pro differentiator."""
    suite = TIER_FEATURES["suite"]
    pro = TIER_FEATURES["pro"]
    # uc_suite_entitlement is the differentiator.
    assert suite["uc_suite_entitlement"] is True
    assert pro["uc_suite_entitlement"] is False
    # Otherwise Suite mirrors Pro on Meeting-Ops audio/text flags.
    for shared in (
        "audio_upload",
        "server_text_storage",
        "ai_chat_over_corpus",
        "cross_meeting_search",
        "canonical_reprocess",
        "qwen36_summary",
        "speaker_library",
        "brigade_integration",
        "bulk_import",
    ):
        assert suite[shared] == pro[shared], (
            f"Suite and Pro should share flag {shared!r} on Meeting-Ops"
        )


def test_enterprise_has_byok_and_suite_entitlement():
    """Enterprise inherits Suite's cross-app entitlement AND adds BYOK."""
    ent = TIER_FEATURES["enterprise"]
    assert ent["uc_suite_entitlement"] is True
    assert ent["byok_models"] is True
    # And only Enterprise should have BYOK.
    for other in ("free", "basic", "pro", "suite"):
        assert TIER_FEATURES[other]["byok_models"] is False


def test_tier_features_agent_write_flags():
    """The new write capabilities should follow the ratified access policy."""
    assert TIER_FEATURES["free"]["agent_write_basic"] is True
    assert TIER_FEATURES["free"]["agent_write_reprocess"] is False
    assert TIER_FEATURES["free"]["agent_write_email_draft"] is False
    assert TIER_FEATURES["pro"]["agent_write_basic"] is True
    assert TIER_FEATURES["pro"]["agent_write_reprocess"] is True
    assert TIER_FEATURES["pro"]["agent_write_email_draft"] is True
    assert TIER_FEATURES["enterprise"]["agent_write_basic"] is True
    assert TIER_FEATURES["enterprise"]["agent_write_reprocess"] is True
    assert TIER_FEATURES["enterprise"]["agent_write_email_draft"] is True


# ---------------------------------------------------------------------------
# get_user_tier
# ---------------------------------------------------------------------------


def test_get_user_tier_returns_column_value():
    """Non-superuser falls through to the tier column."""
    assert get_user_tier(FakeUser(tier="free", is_superuser=False)) == "free"
    assert get_user_tier(FakeUser(tier="pro", is_superuser=False)) == "pro"
    assert get_user_tier(FakeUser(tier="enterprise", is_superuser=False)) == "enterprise"


def test_get_user_tier_superuser_overrides_to_enterprise():
    """is_superuser=True forces enterprise regardless of tier column."""
    assert get_user_tier(FakeUser(tier="free", is_superuser=True)) == "enterprise"
    assert get_user_tier(FakeUser(tier="pro", is_superuser=True)) == "enterprise"


def test_get_user_tier_defaults_to_free_when_missing():
    """A user object without tier set (or with None) defaults to 'free'."""
    assert get_user_tier(FakeUser(tier=None, is_superuser=False)) == "free"


# ---------------------------------------------------------------------------
# get_tier_features
# ---------------------------------------------------------------------------


def test_get_tier_features_returns_dict_for_tier():
    free_features = get_tier_features(FakeUser(tier="free", is_superuser=False))
    assert free_features["server_live"] is False
    assert free_features["diarization"] is False

    pro_features = get_tier_features(FakeUser(tier="pro", is_superuser=False))
    assert pro_features["server_live"] is True
    assert pro_features["hipaa"] is False

    ent_features = get_tier_features(FakeUser(tier="enterprise", is_superuser=False))
    assert ent_features["hipaa"] is True
    assert ent_features["brigade_byok"] is True


def test_get_tier_features_superuser_gets_enterprise_dict():
    """is_superuser=True returns the enterprise feature dict even on a
    user with tier='free' in the column."""
    features = get_tier_features(FakeUser(tier="free", is_superuser=True))
    assert features["hipaa"] is True
    assert features["brigade_byok"] is True


# ---------------------------------------------------------------------------
# require_tier dependency
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine to completion for tests."""
    return asyncio.run(coro)


def test_require_tier_passes_at_or_above_min():
    """A pro user must pass require_tier('pro') and require_tier('free')."""
    checker_pro = require_tier("pro")
    checker_free = require_tier("free")

    user = FakeUser(tier="pro", is_superuser=False)

    # Direct call to inner function (FastAPI would call this via Depends).
    assert _run(checker_pro(current_user=user)) is user
    assert _run(checker_free(current_user=user)) is user


def test_require_tier_403s_below_min():
    """A free user hitting a pro-gated endpoint gets a 403."""
    checker_pro = require_tier("pro")
    free_user = FakeUser(tier="free", is_superuser=False)

    with pytest.raises(HTTPException) as exc_info:
        _run(checker_pro(current_user=free_user))

    assert exc_info.value.status_code == 403
    assert "pro" in exc_info.value.detail


def test_require_tier_superuser_passes_everything():
    """A free-tier user with is_superuser=True passes enterprise gate."""
    checker_ent = require_tier("enterprise")
    superuser = FakeUser(tier="free", is_superuser=True)

    assert _run(checker_ent(current_user=superuser)) is superuser


def test_require_tier_unknown_min_tier_raises_at_definition_time():
    """Typos in the tier name fail at app boot, not on a paying customer."""
    with pytest.raises(ValueError) as exc_info:
        require_tier("ultra")
    assert "min_tier must be one of" in str(exc_info.value)
