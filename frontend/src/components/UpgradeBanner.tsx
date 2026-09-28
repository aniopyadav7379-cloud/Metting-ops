// UpgradeBanner — small dismissible banner for free-tier users.
//
// Renders at the top of Sessions / SessionDetails.
//   - tier gate (the user's account is Free) → the CTA starts Stripe Checkout
//     directly via beginProCheckout('pro').
//   - workspace gate (the user is Pro but the ACTIVE workspace is Free) → the
//     CTA routes to the in-app /pricing page, since a personal Pro checkout
//     does not change a workspace's plan.
// Dismiss persists for 7 days in localStorage (so we don't nag), then it
// comes back on its own.
//
// Price copy is single-sourced from constants/pricing.ts. Browser-first moat
// language: this is the upsell for SERVER-side features. Free is on-device by
// design — see compute-economics.md.

import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Sparkles, X } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { useTierFeatures } from '../hooks/useTierFeatures';
import { beginProCheckout } from '../utils/checkout';
import { PRO_MONTHLY_PRICE_WITH_PERIOD } from '../constants/pricing';

const DISMISS_KEY = 'meet:upgrade-banner:dismissed';
const DISMISS_TTL_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

function isDismissedRecently(): boolean {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return false;
    const ts = parseInt(raw, 10);
    if (!Number.isFinite(ts)) return false;
    return Date.now() - ts < DISMISS_TTL_MS;
  } catch {
    return false;
  }
}

function markDismissed(): void {
  try {
    localStorage.setItem(DISMISS_KEY, String(Date.now()));
  } catch {
    /* ignore storage failures */
  }
}

export const UpgradeBanner: React.FC = () => {
  const { user } = useAuth();
  const { limitedBy, orgName } = useTierFeatures();
  const [dismissed, setDismissed] = useState<boolean>(true);

  // Read storage on mount only — avoids the SSR-style flash on each render.
  useEffect(() => {
    setDismissed(isDismissedRecently());
  }, []);

  // billing-1: server compute is gated by BOTH the user's tier AND the active
  // workspace's plan. Use a representative server-compute capability to decide
  // why the user can't reach the paid features, so the prompt is correct:
  //  - 'tier'      → the user's own plan is Free (upsell the account)
  //  - 'workspace' → the user is paid but the ACTIVE workspace is Free (upsell
  //                  / switch the workspace) — the case the old tier-only gate
  //                  silently hid, leaving "enabled UI → click → 403".
  // Superusers resolve to enterprise + bypass the workspace gate, so they
  // never see this (matches the prior behavior). Anonymous users have /signup.
  if (!user) return null;
  const gate = limitedBy('canonical_reprocess');
  if (!gate) return null;
  if (dismissed) return null;

  const isWorkspaceGate = gate === 'workspace';
  const workspaceLabel = orgName ? `“${orgName}”` : 'this workspace';

  const handleDismiss = () => {
    markDismissed();
    setDismissed(true);
  };

  return (
    <div
      className="relative mb-4 flex flex-col gap-2 rounded-xl border border-fuchsia-400/30 bg-gradient-to-r from-fuchsia-500/10 via-violet-500/10 to-fuchsia-500/10 p-3 text-sm text-zinc-100 sm:flex-row sm:items-center sm:justify-between sm:gap-4 sm:p-4"
      role="region"
      aria-label={isWorkspaceGate ? 'Upgrade workspace' : 'Upgrade to Pro'}
    >
      <div className="flex items-start gap-3 sm:items-center">
        <Sparkles className="mt-0.5 h-5 w-5 shrink-0 text-fuchsia-300 sm:mt-0" />
        {isWorkspaceGate ? (
          <p className="leading-6">
            <strong className="font-semibold text-white">
              {workspaceLabel} is on the Free plan.
            </strong>{' '}
            Your account is upgraded, but server-side processing — diarization, AI chat,
            cross-meeting search — runs against the active workspace's plan. Upgrade {workspaceLabel}{' '}
            (or switch to a paid workspace) to use it here.
          </p>
        ) : (
          <div className="leading-6">
            <p>
              <strong className="font-semibold text-white">
                Upgrade to Pro — {PRO_MONTHLY_PRICE_WITH_PERIOD}.
              </strong>{' '}
              Higher-accuracy transcription, speaker diarization, AI chat, cross-device sync. Cancel anytime.
            </p>
            <p className="mt-1 text-xs text-zinc-400">
              By upgrading you agree to the{' '}
              <Link
                to="/terms"
                className="text-fuchsia-200 underline-offset-4 hover:underline"
              >
                Terms &amp; refund policy
              </Link>
              .
            </p>
          </div>
        )}
      </div>
      <div className="flex items-center gap-2">
        {isWorkspaceGate ? (
          <Link
            to="/pricing"
            className="inline-flex items-center justify-center rounded-lg bg-white px-3 py-2 text-sm font-semibold text-zinc-950 transition hover:bg-zinc-100"
          >
            Upgrade workspace
          </Link>
        ) : (
          <button
            type="button"
            onClick={() => beginProCheckout('upgrade_banner')}
            className="inline-flex items-center justify-center rounded-lg bg-white px-3 py-2 text-sm font-semibold text-zinc-950 transition hover:bg-zinc-100"
          >
            Upgrade to Pro
          </button>
        )}
        <button
          type="button"
          aria-label="Dismiss upgrade banner"
          onClick={handleDismiss}
          className="rounded-md p-1 text-zinc-400 transition hover:bg-white/10 hover:text-white"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
};

export default UpgradeBanner;
