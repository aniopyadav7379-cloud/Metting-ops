"""Tier-based feature gating.

Source of truth for what each tier unlocks. TIER_FEATURES is the dict
every gate consumes; `get_user_tier()` resolves a User to a tier name
(with the is_superuser override to enterprise); `get_tier_features()`
returns the resolved feature dict; `require_tier()` is the FastAPI
dependency factory that 403s requests below a minimum tier.

Wire convention:
  - Backend route gates a feature with `Depends(require_tier("pro"))`
  - Frontend mirrors TIER_FEATURES.free as the default in `useTierFeatures()`
    so UI doesn't briefly render disabled while /api/auth/me is in-flight.
  - is_superuser users always resolve to enterprise. This makes support
    debugging feature flags simple (no need to manually flip tier columns
    on a user every time we want to repro something).

Quota numbers (max_sessions_per_month / max_session_minutes) are encoded
here for completeness but are not yet enforced by any code path. When
the quota service lands it should consume these same numbers via
`get_tier_features()` so there is one canonical source.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Depends, HTTPException

from auth.dependencies import get_current_active_user, get_current_organization
from auth.models import User

logger = logging.getLogger(__name__)


TIER_FEATURES: dict[str, dict[str, Any]] = {
    "free": {
        "server_live": False,
        "diarization": False,
        "live_diarization": False,
        "qwen36_summary": False,
        "speaker_library": False,
        "retention_controls": False,
        "brigade_byok": False,
        "hipaa": False,
        # Server-processing gates (v3.0.0). Free tier is browser-only: it
        # never uploads audio/text chunks, never triggers the canonical
        # server reprocess pipeline, never writes to Brigade, and has no
        # server-side cross-device sync. Everything stays in the browser
        # (on-device STT + LLM + IndexedDB, i.e. Privacy Mode by default).
        "canonical_reprocess": False,
        "brigade_integration": False,
        "cross_device_sync": False,
        "bulk_import": False,
        "agent_write_basic": True,
        "agent_write_reprocess": False,
        "agent_write_email_draft": False,
        # v3.23.0 5-tier matrix gates. Free is fully browser-only: no
        # audio upload, no server text storage, no server-corpus chat /
        # search, no cross-app entitlement, no BYOK.
        "audio_upload": False,
        "server_text_storage": False,
        "ai_chat_over_corpus": False,
        "cross_meeting_search": False,
        "uc_suite_entitlement": False,
        "byok_models": False,
        "max_sessions_per_month": 50,
        "max_session_minutes": 60,
    },
    "basic": {
        # v3.23.0 Basic tier ($7.99/mo, $79/yr). Architectural rule:
        # Basic gets server-side TEXT processing (search, chat-over-corpus,
        # cross-device sync of artifacts). Pro gets server-side AUDIO
        # processing (canonical_reprocess, diarization, summary, speaker
        # library, etc.). A Basic user records on-device just like a Free
        # user, but their transcript/summary/action-items are stored on
        # the server and synced across devices, and they can run AI Chat
        # / search across their library.
        "server_live": False,
        "diarization": False,
        "live_diarization": False,
        # No server-side summary pass: Basic relies on the browser LLM
        # for summarization, then ships the resulting text up to the
        # server for storage + corpus retrieval.
        "qwen36_summary": False,
        # speaker_library depends on canonical_reprocess (server-side
        # diarization + embeddings) — not in Basic.
        "speaker_library": False,
        "retention_controls": False,
        "brigade_byok": False,
        "hipaa": False,
        # NO canonical server reprocess pass — that's the line between
        # Basic (text) and Pro (audio).
        "canonical_reprocess": False,
        # No Brigade graph writes (depends on server-side processed
        # speaker/entity extraction).
        "brigade_integration": False,
        # Text artifacts sync across devices; audio does NOT — that's
        # the Pro-line moat.
        "cross_device_sync": True,
        "bulk_import": False,
        "agent_write_basic": True,
        "agent_write_reprocess": False,
        "agent_write_email_draft": True,
        "audio_upload": False,
        "server_text_storage": True,
        "ai_chat_over_corpus": True,
        "cross_meeting_search": True,
        "uc_suite_entitlement": False,
        "byok_models": False,
        "max_sessions_per_month": None,
        "max_session_minutes": None,
    },
    "pro": {
        "server_live": True,
        "diarization": True,
        # live_diarization (Sortformer streaming speaker labels during the
        # meeting) is intentionally OFF for pro. v3.20.1: per Aaron — the
        # value of live speaker labels is marginal vs the cost (always-on
        # GPU per concurrent stream), and the completion-time pyannote
        # pass already gives Pro full speaker-attributed transcripts at
        # session end. Restricted to enterprise + Founding 100.
        "live_diarization": False,
        "qwen36_summary": True,
        "speaker_library": True,
        "retention_controls": False,
        "brigade_byok": False,
        "hipaa": False,
        "canonical_reprocess": True,
        "brigade_integration": True,
        "cross_device_sync": True,
        "bulk_import": True,
        "agent_write_basic": True,
        "agent_write_reprocess": True,
        "agent_write_email_draft": True,
        "audio_upload": True,
        "server_text_storage": True,
        "ai_chat_over_corpus": True,
        "cross_meeting_search": True,
        "uc_suite_entitlement": False,
        "byok_models": False,
        "max_sessions_per_month": None,  # unlimited
        "max_session_minutes": None,
    },
    "suite": {
        # v3.23.0 Suite tier ($35/mo, $350/yr). Same Meeting-Ops surface
        # as Pro — the "Suite" value-add is `uc_suite_entitlement`, which
        # the sibling UC apps (Project-Ops, Contact-Ops) read to give the
        # user Pro-tier benefits there. That cross-app sync is gated on
        # Brigade federation deploy; until that lands, the entitlement is
        # documentation-only and Meeting-Ops alone honors the flag.
        "server_live": True,
        "diarization": True,
        # live_diarization stays Enterprise / Founding 100 only — Aaron's
        # call on v3.20.1 was that the always-on Sortformer GPU cost only
        # makes sense for the top tier.
        "live_diarization": False,
        "qwen36_summary": True,
        "speaker_library": True,
        "retention_controls": False,
        "brigade_byok": False,
        "hipaa": False,
        "canonical_reprocess": True,
        "brigade_integration": True,
        "cross_device_sync": True,
        "bulk_import": True,
        "agent_write_basic": True,
        "agent_write_reprocess": True,
        "agent_write_email_draft": True,
        "audio_upload": True,
        "server_text_storage": True,
        "ai_chat_over_corpus": True,
        "cross_meeting_search": True,
        "uc_suite_entitlement": True,
        "byok_models": False,
        "max_sessions_per_month": None,
        "max_session_minutes": None,
    },
    "enterprise": {
        "server_live": True,
        "diarization": True,
        "live_diarization": True,
        "qwen36_summary": True,
        "speaker_library": True,
        "retention_controls": True,
        "brigade_byok": True,
        "hipaa": True,
        "canonical_reprocess": True,
        "brigade_integration": True,
        "cross_device_sync": True,
        "bulk_import": True,
        "agent_write_basic": True,
        "agent_write_reprocess": True,
        "agent_write_email_draft": True,
        "audio_upload": True,
        "server_text_storage": True,
        "ai_chat_over_corpus": True,
        "cross_meeting_search": True,
        "uc_suite_entitlement": True,
        "byok_models": True,
        "max_sessions_per_month": None,
        "max_session_minutes": None,
    },
}


# v3.20.1: Founding 100 members inherit enterprise capabilities for the
# features listed here, regardless of their base tier. Aaron's intent:
# perks-only Founding cohort gets early-access compute features (live
# diarization) without paying enterprise pricing. Keep this list TIGHT —
# every flag here costs us on every Founding 100 customer at scale, so
# only add features that are genuinely a "first-look" benefit, not the
# full enterprise SKU. (Founding 100 is NOT a free upgrade to enterprise.)
_FOUNDING_MEMBER_OVERLAY: dict[str, Any] = {
    "live_diarization": True,
}


# All feature keys known across every tier — used to validate feature names
# at app boot in require_feature(). Recomputed once at import.
_KNOWN_FEATURES: frozenset[str] = frozenset().union(
    *(d.keys() for d in TIER_FEATURES.values())
)


# Ordinal rank for tier comparisons. Higher number = more capabilities.
# v3.23.0: 5-tier matrix. Basic slots between free and pro; Suite slots
# between pro and enterprise. Code that does `_TIER_RANK[user_tier] >=
# _TIER_RANK[min_tier]` keeps working unchanged.
_TIER_RANK: dict[str, int] = {
    "free": 0,
    "basic": 1,
    "pro": 2,
    "suite": 3,
    "enterprise": 4,
}


def get_user_tier(user: User) -> str:
    """Return effective tier for a user.

    `is_superuser=True` overrides to `enterprise` regardless of the tier
    column. This way support/dev accounts always get full capability
    without us having to remember to update both flags.
    """
    if getattr(user, "is_superuser", False):
        return "enterprise"
    return getattr(user, "tier", None) or "free"


def get_tier_features(user: User) -> dict[str, Any]:
    """Return the feature dict for a user's effective tier.

    Applies the Founding 100 overlay (`_FOUNDING_MEMBER_OVERLAY`) on top of
    the base tier features when ``user.is_founding_member`` is True. The
    base tier dict is copied, not mutated — TIER_FEATURES stays the
    immutable source of truth for what each plain tier gets.
    """
    base = TIER_FEATURES[get_user_tier(user)]
    if getattr(user, "is_founding_member", False):
        return {**base, **_FOUNDING_MEMBER_OVERLAY}
    return base


def require_tier(min_tier: str):
    """FastAPI dependency factory: 403s if user tier is below min_tier.

    Usage:
        @router.post("/live")
        async def start_live(
            user: User = Depends(require_tier("pro")),
        ):
            ...

    Raises 500 at definition time (not request time) if min_tier is not
    one of the known tier names — catches typos at app boot rather than
    on a request from a paying customer.
    """
    if min_tier not in _TIER_RANK:
        raise ValueError(
            f"require_tier(min_tier={min_tier!r}): "
            f"min_tier must be one of {sorted(_TIER_RANK)}"
        )

    min_rank = _TIER_RANK[min_tier]

    async def checker(
        current_user: User = Depends(get_current_active_user),
    ) -> User:
        user_tier = get_user_tier(current_user)
        if _TIER_RANK[user_tier] < min_rank:
            raise HTTPException(
                status_code=403,
                detail=f"This feature requires {min_tier} tier or higher",
            )
        return current_user

    return checker


def require_feature(feature: str):
    """FastAPI dependency factory: 403s if the user's tier lacks `feature`.

    Like `require_tier` but keys off a named capability in TIER_FEATURES, so
    the gate tracks the feature matrix rather than a hardcoded minimum tier
    (a future plan that grants reprocess but not brigade just flips the
    matrix, no call-site change). Validates the feature name at app boot.

    Usage:
        @router.post("/finalize-audio")
        async def finalize_audio(
            user: User = Depends(require_feature("canonical_reprocess")),
        ):
            ...
    """
    if feature not in _KNOWN_FEATURES:
        raise ValueError(
            f"require_feature(feature={feature!r}): "
            f"unknown feature; known={sorted(_KNOWN_FEATURES)}"
        )

    async def checker(
        current_user: User = Depends(get_current_active_user),
        active_org: Any = Depends(get_current_organization),
    ) -> User:
        if not get_tier_features(current_user).get(feature):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This action needs a paid tier "
                    f"(missing capability: {feature})"
                ),
            )
        # billing-1: server compute must ALSO be covered by the ACTIVE
        # workspace's plan, not just the user's global tier — otherwise one
        # paid seat unlocks compute in every org the buyer belongs to.
        # Superusers (support/dev, who resolve to enterprise) bypass the
        # workspace gate so they can debug inside any org.
        if not getattr(current_user, "is_superuser", False) and not org_covers_feature(
            active_org, feature
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This workspace's plan does not include this capability "
                    f"({feature}). Upgrade the active organization to use it."
                ),
            )
        return current_user

    return checker


def get_org_tier(org: Any) -> str:
    """Return the effective tier for an Organization.

    Mirrors `get_user_tier` for cases where the caller is identified by
    org (e.g. satellite devices that authenticate via device secret and
    resolve to an owning org, not an individual user). The Organization
    model stores the tier in the `plan` column (free / pro / enterprise),
    not `tier`. Defaults to `free` when the column is missing or empty.
    """
    return ((getattr(org, "plan", None) or "free")).lower()


def get_org_tier_features(org: Any) -> dict[str, Any]:
    """Return the feature dict for an Organization's plan tier.

    Org-side counterpart to `get_tier_features`. KeyError-safe: `Organization.
    plan` is meant to be one of free/pro/enterprise (the Stripe webhook's
    `_org_plan_for_tier` only ever writes those), but if a row holds an
    unrecognized plan string (manual edit / cross-node drift) we fail SAFE by
    treating it as `free` and logging loudly, rather than 500-ing a request on
    a `TIER_FEATURES[...]` KeyError. There is deliberately no founding-member
    overlay here — that's a per-USER perk, not a workspace plan.
    """
    tier = get_org_tier(org)
    features = TIER_FEATURES.get(tier)
    if features is None:
        logger.warning(
            "Organization plan %r is not a known tier; gating it as 'free'. "
            "Known tiers: %s",
            tier,
            sorted(TIER_FEATURES),
        )
        return TIER_FEATURES["free"]
    return features


def org_covers_feature(org: Any, feature: str) -> bool:
    """True if the organization's plan tier includes `feature`.

    `org` may be an `Organization`, an `ActiveOrganization` wrapper, or None
    (treated as free → denies paid features). This is the org-side
    authoritative per-workspace billing gate (billing-1).
    """
    resolved = getattr(org, "organization", org)
    return bool(get_org_tier_features(resolved).get(feature))


def gate_feature_for_caller(
    caller: Any, feature: str, active_org: Any = None
) -> None:
    """Inline tier gate for endpoints that resolve the caller themselves.

    Endpoints using `Depends(get_internal_or_user)` get back EITHER a real
    `User` OR an internal-service principal (loopback callers like
    room_recorder). Internal/service callers BYPASS tier gating — they are
    trusted infrastructure, not subject to user billing tiers. A real `User`
    whose effective tier lacks `feature` gets a 403.

    billing-1: when `active_org` is supplied AND the caller is a real
    (non-superuser) User, the ACTIVE WORKSPACE's plan must ALSO cover the
    feature. This makes paid server-compute authoritative on the org being
    charged, not just the buyer's global tier — so one paid seat no longer
    unlocks compute in every org the buyer belongs to. `active_org=None`
    (the default) keeps the legacy user-only behavior, so existing 2-arg
    callers and trusted internal paths are unchanged. `active_org` may be an
    `ActiveOrganization` wrapper or a bare `Organization`.

    Call this right after the caller (and, when applicable, the active org)
    is resolved, before doing any work:
        gate_feature_for_caller(caller, "canonical_reprocess", active_org)
    """
    if feature not in _KNOWN_FEATURES:
        raise ValueError(
            f"gate_feature_for_caller(feature={feature!r}): "
            f"unknown feature; known={sorted(_KNOWN_FEATURES)}"
        )
    # Only real Users are tier-gated. Internal-service principals
    # (InternalServiceCaller, __slots__ = ("provenance",)) carry no `tier`
    # and bypass — they're trusted infra. We duck-type on the `tier`
    # attribute rather than isinstance(User): the test suite reloads
    # auth.models (conftest), which leaves multiple live User class
    # identities and makes isinstance miss. Every real User has `tier`.
    if not hasattr(caller, "tier"):
        return
    if not get_tier_features(caller).get(feature):
        raise HTTPException(
            status_code=403,
            detail=f"This action needs a paid tier (missing capability: {feature})",
        )
    # billing-1 per-workspace gate. Superusers (support/dev, who resolve to
    # enterprise on the user side) also bypass the workspace gate so they can
    # debug inside any org.
    if active_org is not None and not getattr(caller, "is_superuser", False):
        if not org_covers_feature(active_org, feature):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This workspace's plan does not include this capability "
                    f"({feature}). Upgrade the active organization to use it."
                ),
            )
