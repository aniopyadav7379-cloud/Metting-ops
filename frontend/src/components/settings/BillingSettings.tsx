// BillingSettings — the logged-in user's "Plan & billing" panel.
// -----------------------------------------------------------------------------
// This is the reliable in-app entry point for buying Pro and for managing an
// existing subscription:
//   - Free / non-subscriber  → "Upgrade to Pro" (price from pricing.ts) → Stripe Checkout.
//   - Existing subscriber     → "Manage subscription"     → Stripe Billing Portal
//                               (the cancel / refund / update-card surface).
//
// The Stripe wiring lives in utils/checkout.ts (which wraps the live
// services/billingApi.ts client). This component only renders state + buttons.
// -----------------------------------------------------------------------------

import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  BadgeCheck,
  Check,
  CreditCard,
  ExternalLink,
  RefreshCw,
  Sparkles,
} from 'lucide-react';
import { useAuth } from '../../contexts/AuthContext';
import {
  fetchSubscription,
  type SubscriptionState,
} from '../../services/billingApi';
import {
  beginProCheckout,
  isPaidTier,
  manageSubscription,
} from '../../utils/checkout';
import { PRO_MONTHLY_PRICE_WITH_PERIOD } from '../../constants/pricing';

const PRO_FEATURES = [
  'Server pass: studio transcripts + speaker diarization',
  'Polished AI summaries with action items & decisions',
  'Cross-meeting AI chat + semantic search',
  'Person-centric knowledge graph',
];

function formatDate(iso?: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleDateString();
}

/** Short "By upgrading you agree…" line shown next to every buy/manage CTA. */
function TermsNote() {
  return (
    <p className="mt-3 text-xs text-zinc-500">
      By upgrading you agree to the{' '}
      <Link
        to="/terms"
        className="text-fuchsia-300 underline-offset-4 hover:underline"
      >
        Terms
      </Link>{' '}
      &amp;{' '}
      <Link
        to="/terms"
        className="text-fuchsia-300 underline-offset-4 hover:underline"
      >
        refund policy
      </Link>
      . Cancel anytime.
    </p>
  );
}

export default function BillingSettings() {
  const { tier } = useAuth();
  const [sub, setSub] = useState<SubscriptionState | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      try {
        const state = await fetchSubscription();
        if (!cancelled) setSub(state);
      } catch {
        // Billing unconfigured or a transient error — fall back to the
        // tier-derived view (still lets a free user reach checkout).
        if (!cancelled) setSub(null);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // "Already paying" = a live subscription OR a resolved paid tier.
  const hasSubscription = Boolean(sub?.has_subscription) || isPaidTier(tier);
  const renewsOn = formatDate(sub?.current_period_end);

  const handleUpgrade = async () => {
    setBusy(true);
    try {
      await beginProCheckout('settings_billing');
    } finally {
      // On success we've already navigated away; this only matters if the
      // helper toasted an error and we stayed on the page.
      setBusy(false);
    }
  };

  const handleManage = async () => {
    setBusy(true);
    try {
      await manageSubscription();
    } finally {
      setBusy(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-8 text-sm text-zinc-500">
        <RefreshCw className="h-4 w-4 animate-spin" />
        Loading your plan…
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Current plan summary */}
      <div className="rounded-lg border border-white/10 bg-zinc-900/60 p-4 sm:p-6">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-fuchsia-500/15">
              <CreditCard className="h-5 w-5 text-fuchsia-300" />
            </div>
            <div>
              <h3 className="text-base font-semibold text-white">Your plan</h3>
              <p className="text-sm text-zinc-400">
                {hasSubscription
                  ? 'You’re on a paid plan.'
                  : 'You’re on the Free plan.'}
              </p>
            </div>
          </div>
          <span
            className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${
              hasSubscription
                ? 'bg-emerald-500/15 text-emerald-300'
                : 'bg-zinc-700/50 text-zinc-300'
            }`}
          >
            {hasSubscription ? (
              <>
                <BadgeCheck className="h-3.5 w-3.5" />
                {(tier ?? 'pro').toUpperCase()}
              </>
            ) : (
              'FREE'
            )}
          </span>
        </div>

        {hasSubscription && (
          <div className="mt-4 flex flex-wrap gap-x-6 gap-y-1 text-sm text-zinc-300">
            {sub?.status && (
              <span>
                <span className="text-zinc-500">Status:</span> {sub.status}
              </span>
            )}
            {renewsOn && (
              <span>
                <span className="text-zinc-500">
                  {sub?.cancel_at_period_end ? 'Ends:' : 'Renews:'}
                </span>{' '}
                {renewsOn}
              </span>
            )}
          </div>
        )}

        {sub?.test_mode && (
          <p className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
            Billing is in test mode on this instance — no real charges are made.
          </p>
        )}
      </div>

      {/* Action card: upgrade OR manage */}
      {hasSubscription ? (
        <div className="rounded-lg border border-white/10 bg-zinc-900/60 p-4 sm:p-6">
          <h3 className="text-base font-semibold text-white">Manage subscription</h3>
          <p className="mt-1 text-sm text-zinc-400">
            Update your payment method, download invoices, or cancel your plan in
            the secure Stripe billing portal.
          </p>
          <button
            type="button"
            onClick={handleManage}
            disabled={busy}
            className="mt-4 inline-flex items-center gap-2 rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm font-semibold text-zinc-100 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <ExternalLink className="h-4 w-4" />
            Manage subscription
          </button>
          <TermsNote />
        </div>
      ) : (
        <div className="rounded-lg border border-fuchsia-400/30 bg-gradient-to-br from-fuchsia-500/10 to-violet-500/10 p-4 sm:p-6">
          <div className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-fuchsia-300" />
            <h3 className="text-base font-semibold text-white">
              Upgrade to Pro — {PRO_MONTHLY_PRICE_WITH_PERIOD}
            </h3>
          </div>
          <p className="mt-1 text-sm text-zinc-300">
            Add the GPU server pass and the intelligence layer across your whole
            library.
          </p>
          <ul className="mt-4 space-y-2 text-sm text-zinc-200">
            {PRO_FEATURES.map((f) => (
              <li key={f} className="flex items-start gap-2.5">
                <Check className="mt-0.5 h-4 w-4 shrink-0 text-fuchsia-300" />
                <span>{f}</span>
              </li>
            ))}
          </ul>
          <button
            type="button"
            onClick={handleUpgrade}
            disabled={busy}
            className="mt-5 inline-flex items-center gap-2 rounded-xl bg-gradient-to-r from-fuchsia-500 to-violet-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-fuchsia-500/20 transition hover:from-fuchsia-400 hover:to-violet-400 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy ? (
              <RefreshCw className="h-4 w-4 animate-spin" />
            ) : (
              <Sparkles className="h-4 w-4" />
            )}
            Upgrade to Pro
          </button>
          <TermsNote />
        </div>
      )}
    </div>
  );
}
