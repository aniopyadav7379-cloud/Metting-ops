import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  CalendarCheck2,
  Crown,
  Gift,
  Headphones,
  Lock,
  Mail,
  MessageSquare,
  Sparkles,
  Vote,
} from 'lucide-react';
import { config } from '../config';

// Public Founding 100 page (v3.21.0 → v3.22.5).
//
// Fetches GET /api/founding/status?cohort=meeting_ops_v1 (no auth, cached
// 60s).
//
// v3.22.5 reframe: the Founding 100 cohort is "Coming soon" until the
// packaging is ready. The backend grant mechanism stays armed — the
// /api/founding/status endpoint still exposes `is_open`, and the Stripe
// webhook still grants on annual checkout when the cohort flips open —
// but we don't surface a Subscribe CTA here. Instead we collect a
// waitlist via POST /api/landing/invite-request, same endpoint the
// landing page uses. When we open the cohort, we re-mail the list
// before turning the public CTA back on.
//
// The seat counter stays visible (reframed as "early supporters") so the
// two already-backfilled seats don't look weird while "coming soon."

interface FoundingStatus {
  cohort: string;
  seats_taken: number;
  seats_total: number;
  is_open: boolean;
}

interface Perk {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  body: string;
}

const PERKS: Perk[] = [
  {
    icon: Lock,
    title: 'Lifetime price-lock',
    body: 'Your annual rate is fixed for as long as you stay subscribed. No surprise hikes when we re-price.',
  },
  {
    icon: MessageSquare,
    title: 'Private Discord',
    body: 'Direct line to the team in a small, focused room. Bug reports, roadmap pushback, build threads.',
  },
  {
    icon: Crown,
    title: 'Advisory council seat',
    body: 'Quarterly call with the founder. Steer what gets built next.',
  },
  {
    icon: Vote,
    title: 'Quarterly roadmap vote',
    body: 'Weighted ballot on the next quarter\'s product surface — you pick the work, we ship it.',
  },
  {
    icon: CalendarCheck2,
    title: 'Annual founders summit',
    body: 'One day a year, in person. Talks, demos, working sessions. Travel on you, room and meals on us.',
  },
  {
    icon: Sparkles,
    title: 'Cross-app early access',
    body: 'Project-Ops, Contact-Ops, Accounting-Ops, Brigade — you see new ecosystem apps before public release.',
  },
];

export const Founding100: React.FC = () => {
  const [status, setStatus] = useState<FoundingStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waitlistEmail, setWaitlistEmail] = useState('');
  const [waitlistState, setWaitlistState] = useState<
    'idle' | 'submitting' | 'submitted' | 'error'
  >('idle');
  const [waitlistError, setWaitlistError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const r = await fetch(
          `${config.apiBaseUrl}/api/founding/status?cohort=meeting_ops_v1`,
        );
        if (!r.ok) {
          throw new Error(`status ${r.status}`);
        }
        const body = (await r.json()) as FoundingStatus;
        if (!cancelled) setStatus(body);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Could not load cohort status.');
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  const seatsTaken = status?.seats_taken ?? 0;
  // While "coming soon" we still surface the seat count, but reframed
  // as supporters and counting (not "/ 100 filled") so the backfilled
  // seats don't read as awkward during the closed period.
  const isOpen = status?.is_open ?? false;

  async function handleWaitlistSubmit(e: React.FormEvent) {
    e.preventDefault();
    setWaitlistError(null);
    const email = waitlistEmail.trim();
    if (!email) {
      setWaitlistError('Please enter a valid email address.');
      return;
    }
    setWaitlistState('submitting');
    try {
      // POST to /api/landing/invite-request — the same endpoint the
      // landing page uses. When we open the cohort, we'll re-mail this
      // list before flipping the public CTA. The endpoint is idempotent
      // server-side (silently no-ops on duplicates), so re-submits are safe.
      const r = await fetch(`${config.apiBaseUrl}/api/landing/invite-request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
      });
      if (!r.ok) {
        if (r.status === 429) {
          throw new Error('Too many requests — try again in a few minutes.');
        }
        let detail = `${r.status} ${r.statusText}`;
        try {
          const data = await r.json();
          if (data?.detail) detail = data.detail;
        } catch {
          /* ignore */
        }
        throw new Error(detail);
      }
      setWaitlistState('submitted');
    } catch (err) {
      setWaitlistState('error');
      setWaitlistError(
        err instanceof Error ? err.message : 'Could not submit. Try again in a moment.',
      );
    }
  }

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-4 py-8 lg:px-8">
        <header className="flex items-center justify-between border-b border-white/10 pb-5">
          <Link to="/" className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-500 shadow-lg shadow-fuchsia-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="text-sm font-semibold tracking-tight text-white">Meeting-Ops</div>
              <div className="text-xs text-zinc-400">Founding 100</div>
            </div>
          </Link>
          <div className="flex items-center gap-3 text-sm text-zinc-300">
            <Link to="/pricing" className="rounded-full border border-white/10 px-3 py-2 hover:border-fuchsia-400/40 hover:text-white">
              Pricing
            </Link>
            <Link to="/login" className="rounded-full border border-white/10 px-3 py-2 hover:border-fuchsia-400/40 hover:text-white">
              Sign in
            </Link>
          </div>
        </header>

        <main className="flex-1 py-10">
          <section className="space-y-6">
            <div className="inline-flex items-center gap-2 rounded-full border border-amber-500/30 bg-amber-500/10 px-3 py-1 text-xs font-medium text-amber-200">
              <Crown className="h-3.5 w-3.5" />
              Meeting-Ops v1 cohort · Coming soon
            </div>
            <div className="space-y-4">
              <h1 className="text-4xl font-semibold tracking-tight text-white sm:text-5xl">
                Founding 100 — Coming soon.
              </h1>
              <p className="max-w-2xl text-base leading-7 text-zinc-300">
                We&rsquo;re putting together a package worthy of the first hundred
                believers. The cohort opens when it&rsquo;s right. Stay close —
                we&rsquo;ll announce it before we open seats.
              </p>
            </div>

            <div className="rounded-3xl border border-white/10 bg-zinc-900/80 p-6 shadow-2xl shadow-black/30 backdrop-blur">
              {error && (
                <div className="mb-4 rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100">
                  We could not reach the supporter counter ({error}). Try again in a minute.
                </div>
              )}
              <div className="flex items-end justify-between gap-4">
                <div>
                  <div className="text-sm font-medium text-zinc-300">Early supporters</div>
                  <div className="mt-1 text-4xl font-semibold tracking-tight text-white">
                    {seatsTaken}{' '}
                    <span className="text-base font-normal text-zinc-400">
                      and counting
                    </span>
                  </div>
                </div>
                <div className="text-right text-sm text-zinc-400">
                  Cohort:&nbsp;
                  <span className="font-mono text-zinc-200">meeting_ops_v1</span>
                </div>
              </div>

              <form
                onSubmit={handleWaitlistSubmit}
                className="mt-6 flex flex-col gap-3 sm:flex-row"
              >
                <label htmlFor="founding-waitlist-email" className="sr-only">
                  Email address
                </label>
                <input
                  id="founding-waitlist-email"
                  type="email"
                  required
                  autoComplete="email"
                  inputMode="email"
                  value={waitlistEmail}
                  onChange={(e) => setWaitlistEmail(e.target.value)}
                  placeholder="you@work.com"
                  disabled={waitlistState === 'submitting' || waitlistState === 'submitted'}
                  className="flex-1 rounded-xl border border-white/10 bg-zinc-950/60 px-4 py-3 text-sm text-zinc-100 placeholder:text-zinc-500 focus:border-fuchsia-400/60 focus:outline-none focus:ring-1 focus:ring-fuchsia-400/40 disabled:cursor-not-allowed disabled:opacity-60"
                />
                <button
                  type="submit"
                  disabled={waitlistState === 'submitting' || waitlistState === 'submitted'}
                  className="inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-fuchsia-500 to-violet-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-fuchsia-500/20 transition hover:from-fuchsia-400 hover:to-violet-400 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <Mail className="h-4 w-4" />
                  {waitlistState === 'submitting'
                    ? 'Saving…'
                    : waitlistState === 'submitted'
                      ? 'On the list'
                      : 'Notify me'}
                </button>
              </form>

              {waitlistState === 'submitted' && (
                <div
                  className="mt-3 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100"
                  role="status"
                >
                  You&rsquo;re on the list. We&rsquo;ll email{' '}
                  <span className="font-mono text-emerald-200">
                    {waitlistEmail.trim()}
                  </span>{' '}
                  before we open the cohort.
                </div>
              )}
              {waitlistState === 'error' && waitlistError && (
                <div
                  className="mt-3 rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100"
                  role="alert"
                >
                  {waitlistError}
                </div>
              )}

              {isOpen && (
                // Defensive: backend flipped the cohort open without us
                // shipping the new package. Surface a quiet inline link
                // rather than failing closed — Aaron's call to keep the
                // grant gate armed, just not promoted here.
                <div className="mt-4 text-sm text-zinc-400">
                  Already an annual customer?{' '}
                  <Link
                    to="/pricing"
                    className="text-fuchsia-200 underline-offset-4 hover:underline"
                  >
                    Go to pricing
                  </Link>
                  .
                </div>
              )}
            </div>
          </section>

          <section className="mt-12 space-y-6">
            <div className="space-y-2">
              <div className="inline-flex items-center gap-2 rounded-full border border-emerald-500/30 bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-200">
                <Gift className="h-3.5 w-3.5" />
                What it will include
              </div>
              <h2 className="text-2xl font-semibold tracking-tight text-white">
                Six perks reserved for the first 100.
              </h2>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              {PERKS.map((perk) => {
                const Icon = perk.icon;
                return (
                  <div
                    key={perk.title}
                    className="rounded-2xl border border-white/10 bg-white/5 p-5 transition hover:border-fuchsia-400/30"
                  >
                    <div className="flex items-center gap-2 text-white">
                      <Icon className="h-5 w-5 text-fuchsia-300" />
                      <span className="font-medium">{perk.title}</span>
                    </div>
                    <p className="mt-2 text-sm leading-6 text-zinc-300">{perk.body}</p>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="mt-12 rounded-3xl border border-white/10 bg-zinc-900/60 p-6">
            <div className="flex items-start gap-3">
              <Headphones className="mt-0.5 h-5 w-5 text-fuchsia-300" />
              <div>
                <div className="font-medium text-white">Questions before you commit?</div>
                <p className="mt-1 text-sm text-zinc-300">
                  Email{' '}
                  <a
                    className="text-fuchsia-200 hover:text-fuchsia-100"
                    href="mailto:hello@magicunicorn.dev?subject=Founding%20100"
                  >
                    hello@magicunicorn.dev
                  </a>
                  . We answer every message from a real person.
                </p>
              </div>
            </div>
          </section>
        </main>
      </div>
    </div>
  );
};

export default Founding100;
