import { useMemo } from 'react';
import {
  DEFAULT_TIER_FEATURES,
  useAuth,
  type Tier,
  type TierFeatures,
} from '../contexts/AuthContext';

/**
 * Hook for tier-based feature gating in components.
 *
 * Returns the EFFECTIVE capability for the CURRENT context = the more-
 * restrictive of (the user's global tier, the ACTIVE workspace's plan).
 * This mirrors the backend gate (billing-1): `require_feature` /
 * `gate_feature_for_caller` require BOTH `tier_features[feature]` (the user)
 * AND `active_org_features[feature]` (the workspace) — so a Pro user acting
 * in a Free workspace is denied server compute. Without this AND the UI would
 * show an enabled control that the backend then 403s.
 *
 * Usage:
 *   const { hasFeature, limitedBy } = useTierFeatures();
 *   if (!hasFeature('server_live')) {
 *     // limitedBy('server_live') === 'workspace' -> prompt to upgrade the org
 *     // limitedBy('server_live') === 'tier'      -> prompt to upgrade the user
 *   }
 *
 * Superusers bypass the workspace gate (support/dev), matching the backend.
 * Before `/api/auth/me` lands the hook falls back to the free defaults so the
 * UI stays conservative.
 */
export type FeatureLimit = 'tier' | 'workspace' | null;

export interface UseTierFeaturesReturn {
  tier: Tier;
  /** EFFECTIVE capabilities = AND(user tier features, active-workspace plan features). */
  features: TierFeatures;
  hasFeature: (key: keyof TierFeatures) => boolean;
  /**
   * Why a feature is gated, so the UI can prompt for the right upgrade:
   *  - 'tier'      → the user's own plan lacks it (upgrade your account)
   *  - 'workspace' → the user has it, but the ACTIVE workspace's plan does not
   *                  (upgrade / switch the workspace)
   *  - null        → not gated
   */
  limitedBy: (key: keyof TierFeatures) => FeatureLimit;
  /** The active workspace's plan (free/pro/enterprise), when known. */
  orgPlan?: string;
  /** The active workspace's display name, for upgrade copy. */
  orgName?: string;
}

export function useTierFeatures(): UseTierFeaturesReturn {
  const { user } = useAuth();

  return useMemo<UseTierFeaturesReturn>(() => {
    const tier: Tier = user?.tier ?? 'free';
    const userFeatures: TierFeatures = user?.tier_features ?? DEFAULT_TIER_FEATURES;
    const isSuperuser = Boolean(user?.is_superuser);

    // The active workspace's capability dict (full plan feature set from the
    // backend). Absent/empty — before /api/auth/me lands, or a user with no
    // active org — means "no workspace restriction". Superusers bypass it.
    const orgFeatures = user?.active_org_features;
    const hasOrgGate =
      !isSuperuser && !!orgFeatures && Object.keys(orgFeatures).length > 0;

    // True when the active workspace covers `key`. No gate / unknown key =>
    // don't restrict (the backend dict carries all keys; this just keeps the
    // UI safe against a frontend/backend key drift rather than over-gating).
    const orgCovers = (key: keyof TierFeatures): boolean =>
      !hasOrgGate ||
      !(key in (orgFeatures as object)) ||
      Boolean(orgFeatures?.[key]);

    // Start from the user's features (this preserves non-boolean keys like the
    // numeric quotas max_sessions_per_month / max_session_minutes) and AND only
    // the BOOLEAN capability flags with the active workspace's plan.
    const effective: TierFeatures = { ...userFeatures };
    (Object.keys(userFeatures) as (keyof TierFeatures)[]).forEach((key) => {
      if (typeof userFeatures[key] === 'boolean') {
        (effective as unknown as Record<string, unknown>)[key] =
          Boolean(userFeatures[key]) && orgCovers(key);
      }
    });

    const limitedBy = (key: keyof TierFeatures): FeatureLimit => {
      if (!userFeatures[key]) return 'tier';
      if (!orgCovers(key)) return 'workspace';
      return null;
    };

    return {
      tier,
      features: effective,
      hasFeature: (key: keyof TierFeatures) => Boolean(effective[key]),
      limitedBy,
      orgPlan: user?.active_organization?.plan,
      orgName: user?.active_organization?.name,
    };
  }, [user]);
}
