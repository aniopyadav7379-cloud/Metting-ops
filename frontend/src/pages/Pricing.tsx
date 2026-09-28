import React, { useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowRight, Check, ExternalLink, Sparkles } from 'lucide-react';
import { track } from '../utils/posthog';
import { useAuth } from '../contexts/AuthContext';
import {
  beginProCheckout,
  isPaidTier,
  manageSubscription,
} from '../utils/checkout';
import {
  PRO_MONTHLY_PRICE_DISPLAY,
  PRO_MONTHLY_PRICE_WITH_PERIOD,
  PRO_LIST_PRICE_DISPLAY,
  PRO_LAUNCH_DISCOUNT_PCT,
} from '../constants/pricing';
import LegalFooter from '../components/LegalFooter';

/**
 * In-app pricing page. Previously this route hard-redirected to the external
 * Unicorn Commander storefront; it now renders a real Free vs Pro view whose
 * Pro CTA drives the LIVE in-app checkout:
 *   - anonymous visitor  → /signup?intent=pro (Stripe Checkout requires auth)
 *   - signed-in, non-Pro → beginProCheckout('pro') → Stripe Checkout
 *   - signed-in, Pro     → manageSubscription() → Stripe Billing Portal
 *
 * All Pro pricing copy is single-sourced from constants/pricing.ts. The amount
 * actually charged is resolved server-side by the backend checkout endpoint.
 */

const FREE_FEATURES = [
  'On-device live transcript + rolling summary',
  'Full session history, searchable',
  'Runs in your browser — private by default',
  'No credit card, no time limit',
];

const PRO_FEATURES = [
  'Everything in Free',
  'Server pass: studio transcripts + speaker diarization',
  'Polished AI summaries with action items & decisions',
  'Cross-meeting AI chat + semantic search',
  'Person-centric knowledge graph',
];

export const Pricing: React.FC = () => {
  const navigate = useNavigate();
  const { isAuthenticated, tier } = useAuth();
  const isPro = isAuthenticated && isPaidTier(tier);

  useEffect(() => {
    track('pricing_viewed', { authenticated: isAuthenticated, tier: tier ?? 'anonymous' });
  }, [isAuthenticated, tier]);

  const handlePro = () => {
    if (!isAuthenticated) {
      // Checkout needs a JWT — send anonymous visitors to signup first.
      navigate('/signup?intent=pro');
      return;
    }
    if (isPro) {
      void manageSubscription();
      return;
    }
    void beginProCheckout('pricing_page');
  };

  const proCtaLabel = !isAuthenticated
    ? 'Get Pro'
    : isPro
      ? 'Manage subscription'
      : 'Upgrade to Pro';

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-4 py-6 lg:px-8">
        {/* Header */}
        <header className="flex items-center justify-between border-b border-white/10 pb-4">
          <Link to="/" className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-500 shadow-lg shadow-fuchsia-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="text-sm font-semibold tracking-tight text-white">Meeting-Ops</div>
              <div className="text-xs text-zinc-400">Pricing</div>
            </div>
          </Link>
          {isAuthenticated ? (
            <Link
              to="/record"
              className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white"
            >
              Back to app
            </Link>
          ) : (
            <Link
              to="/login"
              className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white"
            >
              Sign in
            </Link>
          )}
        </header>

        <main className="flex-1 py-10">
          <div className="mx-auto max-w-2xl text-center">
            <h1 className="text-3xl font-semibold tracking-tight text-white sm:text-4xl">
              Free forever. Pay only for the heavy lifting.
            </h1>
            <p className="mt-4 text-base leading-7 text-zinc-400">
              The live, on-device experience is free for good. Pro adds the GPU
              server pass and the intelligence layer across your whole library.
            </p>
          </div>

          <div className="mx-auto mt-12 grid max-w-3xl gap-6 md:grid-cols-2">
            {/* Free */}
            <div className="flex h-full flex-col rounded-3xl border border-white/10 bg-white/[0.03] p-8">
              <h2 className="text-xl font-semibold text-white">Free</h2>
              <p className="mt-1 text-sm text-zinc-400">
                Everything you need to never take meeting notes again.
              </p>
              <div className="mt-5 flex items-end gap-1">
                <span className="text-5xl font-semibold tabular-nums text-white">$0</span>
                <span className="pb-1 text-sm text-zinc-500">/forever</span>
              </div>
              <ul className="mt-6 space-y-3 text-sm text-zinc-300">
                {FREE_FEATURES.map((f) => (
                  <li key={f} className="flex items-start gap-2.5">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-zinc-500" />
                    <span>{f}</span>
                  </li>
                ))}
              </ul>
              <div className="mt-auto pt-8">
                {isAuthenticated ? (
                  <Link
                    to="/record"
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/5 px-5 py-3 text-sm font-semibold text-zinc-100 transition hover:bg-white/10"
                  >
                    Go to app
                  </Link>
                ) : (
                  <Link
                    to="/signup"
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/5 px-5 py-3 text-sm font-semibold text-zinc-100 transition hover:bg-white/10"
                  >
                    Start free
                  </Link>
                )}
              </div>
            </div>

            {/* Pro */}
            <div className="relative flex h-full flex-col rounded-3xl border border-fuchsia-500/40 bg-zinc-900 p-8 shadow-[0_0_60px_-15px_rgba(217,70,239,0.25)]">
              <span className="absolute -top-3 right-6 rounded-full bg-gradient-to-r from-fuchsia-500 to-violet-500 px-3 py-1 text-[10px] font-semibold uppercase tracking-widest text-white">
                Most popular
              </span>
              <h2 className="text-xl font-semibold text-white">Pro</h2>
              <p className="mt-1 text-sm text-zinc-400">
                Studio-quality output and an AI that knows your whole library.
              </p>
              <div className="mt-5 flex items-end gap-2">
                <span className="pb-1 text-2xl font-medium text-zinc-500 line-through decoration-zinc-600">
                  {PRO_LIST_PRICE_DISPLAY}
                </span>
                <span className="text-5xl font-semibold tabular-nums text-white">
                  {PRO_MONTHLY_PRICE_DISPLAY}
                </span>
                <span className="pb-1 text-sm text-zinc-500">/month</span>
              </div>
              <p className="mt-1 text-xs font-semibold text-fuchsia-300">
                Launch pricing — {PRO_LAUNCH_DISCOUNT_PCT}% off
              </p>
              <ul className="mt-6 space-y-3 text-sm text-zinc-200">
                {PRO_FEATURES.map((f) => (
                  <li key={f} className="flex items-start gap-2.5">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-fuchsia-400" />
                    <span>{f}</span>
                  </li>
                ))}
              </ul>
              <div className="mt-auto pt-8">
                <button
                  type="button"
                  onClick={handlePro}
                  className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-fuchsia-500 to-violet-500 px-5 py-3 text-sm font-semibold text-white shadow-lg shadow-fuchsia-500/20 transition hover:from-fuchsia-400 hover:to-violet-400"
                >
                  {isPro ? <ExternalLink className="h-4 w-4" /> : <ArrowRight className="h-4 w-4" />}
                  {proCtaLabel}
                </button>
                <p className="mt-3 text-center text-xs text-zinc-500">
                  By upgrading you agree to our{' '}
                  <Link to="/terms" className="text-fuchsia-300 underline-offset-4 hover:underline">
                    Terms
                  </Link>{' '}
                  &amp;{' '}
                  <Link to="/terms" className="text-fuchsia-300 underline-offset-4 hover:underline">
                    refund policy
                  </Link>
                  . {PRO_MONTHLY_PRICE_WITH_PERIOD}, cancel anytime.
                </p>
              </div>
            </div>
          </div>

          {/* Existing-subscriber manage link */}
          {isPro && (
            <p className="mt-8 text-center text-sm text-zinc-400">
              Need to update your card or cancel?{' '}
              <button
                type="button"
                onClick={() => void manageSubscription()}
                className="font-medium text-fuchsia-300 underline-offset-4 hover:underline"
              >
                Manage your subscription
              </button>
              .
            </p>
          )}

          <p className="mt-8 text-center text-xs text-zinc-500">
            Need on-prem, an appliance, or a HIPAA BAA?{' '}
            <a
              href="https://unicorncommander.ai/pricing"
              target="_blank"
              rel="noopener noreferrer"
              className="text-fuchsia-300 underline-offset-4 hover:underline"
            >
              See full pricing on the Unicorn Commander storefront →
            </a>
          </p>
        </main>

        <LegalFooter />
      </div>
    </div>
  );
};

export default Pricing;
