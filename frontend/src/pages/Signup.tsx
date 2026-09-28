import React, { useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  Mail,
  Sparkles,
  Ticket,
  UserRoundPlus,
} from 'lucide-react';
import { config } from '../config';
import LegalFooter from '../components/LegalFooter';
import { track } from '../utils/posthog';

function extractApiError(payload: unknown, fallback: string): string {
  if (!payload || typeof payload !== 'object') return fallback;
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === 'string') return item;
        if (item && typeof item === 'object') {
          const message = (item as { msg?: unknown; message?: unknown }).msg ?? (item as { msg?: unknown; message?: unknown }).message;
          if (typeof message === 'string') return message;
        }
        return JSON.stringify(item);
      })
      .join(' ');
  }
  return fallback;
}

export const Signup: React.FC = () => {
  const location = useLocation();
  // Arrived from a "Get Pro" CTA (?intent=pro). Presentational only: sets
  // expectations that the account starts Free and Pro is one step away after
  // sign-in. Actually carrying the intent through to auto-open checkout after
  // email-verify + login is a follow-up (needs auth-flow state), not wired here.
  const wantsProIntent =
    new URLSearchParams(location.search || '').get('intent') === 'pro';
  const [fullName, setFullName] = useState('');
  const [email, setEmail] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  // Phase 1 private beta: GET /api/invite-codes/config tells us whether a
  // valid invite code is REQUIRED to self-serve register. Default to
  // `false` so the field renders as optional until the (anonymous) config
  // probe resolves — never block the form on a slow/failed probe.
  const [requireInviteCode, setRequireInviteCode] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);
  // v3.21.0: when ALLOW_REGISTRATION=false the backend returns
  // 403 "Registration is disabled" on /api/auth/register. The Landing
  // CTA always lands us here, so we detect that case and switch to a
  // friendly "thanks, we got your interest" surface instead of an
  // error toast — the email was already captured by the Landing form
  // posting to /api/landing/invite-request before redirecting.
  const [registrationClosed, setRegistrationClosed] = useState(false);
  // Legal clickwrap: required before account creation. Enforced client-side
  // for v1 (backend persistence of `accepted_terms_at` is a follow-up — the
  // registration path has no column for it yet).
  const [termsAccepted, setTermsAccepted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Pre-fill email from `?email=` query (set by Landing CTA), and the
  // invite code from `?code=` / `?invite=` so an invite link can deep-link
  // the field prefilled.
  useEffect(() => {
    const params = new URLSearchParams(location.search || '');
    const fromQuery = params.get('email');
    if (fromQuery) {
      setEmail(fromQuery.trim());
    }
    const codeFromQuery = params.get('code') || params.get('invite');
    if (codeFromQuery) {
      setInviteCode(codeFromQuery.trim());
    }
  }, [location.search]);

  // Probe the (anonymous-callable) invite-code config on mount so we know
  // whether to mark the invite-code field required. Soft-fail: if the
  // probe errors we leave the field optional rather than blocking signup.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${config.apiUrl}/api/invite-codes/config`, {
          headers: { Accept: 'application/json' },
        });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) {
          setRequireInviteCode(Boolean(data?.require_invite_code));
        }
      } catch {
        /* soft-fail — field stays optional */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);

    if (!termsAccepted) {
      setError('Please accept the Terms, Privacy Policy, and Acceptable Use Policy to continue.');
      return;
    }

    if (password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    setIsSubmitting(true);

    try {
      const response = await fetch(`${config.apiUrl}/api/auth/register`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify({
          email: email.trim(),
          username: username.trim(),
          password,
          ...(fullName.trim() ? { full_name: fullName.trim() } : {}),
          // Phase 1 private beta: redeeming a valid code comps the new
          // user's personal org to Pro (handled server-side). Only send a
          // non-empty trimmed value so the optional path stays clean.
          ...(inviteCode.trim() ? { invite_code: inviteCode.trim() } : {}),
        }),
      });

      if (response.status === 403) {
        if (requireInviteCode) {
          // Invite-code beta is ON: a 403 means the code was missing,
          // invalid, already redeemed, or inactive. Surface the backend's
          // detail message inline so the user can correct the code (rather
          // than the "registration closed" pivot, which is the
          // ALLOW_REGISTRATION=false case below).
          const payload = await response.json().catch(() => null);
          throw new Error(
            extractApiError(payload, 'That invite code could not be used. Check the code and try again.'),
          );
        }
        // Registration is closed (no invite-code gate, ALLOW_REGISTRATION
        // off). Pivot to the friendly invite-confirmation surface — the
        // Landing form has already logged the email server-side. Best-effort
        // re-log here too in case the user typed a fresh email on the form.
        try {
          await fetch(`${config.apiUrl}/api/landing/invite-request`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ email: email.trim() }),
          });
        } catch {
          /* soft-fail — the user-facing message is the same either way */
        }
        setRegistrationClosed(true);
        return;
      }

      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        throw new Error(extractApiError(payload, `Signup failed (${response.status})`));
      }

      setSuccess(true);
      // `via` = 'invite' when the user arrived on a prefilled invite link
      // (?email=...), 'direct' for an organic signup form hit.
      const cameFromInvite = new URLSearchParams(location.search || '').has('email');
      track('signup_completed', { tier: 'free', via: cameFromInvite ? 'invite' : 'direct' });
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : 'Signup failed');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col px-4 py-6 lg:px-8">
        <header className="flex items-center justify-between border-b border-white/10 pb-4">
          <Link to="/pricing" className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-500 shadow-lg shadow-fuchsia-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="text-sm font-semibold tracking-tight text-white">Meeting-Ops</div>
              <div className="text-xs text-zinc-400">Create a consumer account</div>
            </div>
          </Link>
          <Link to="/login" className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white">
            Sign in
          </Link>
        </header>

        <main className="grid flex-1 items-center gap-10 py-10 lg:grid-cols-[1.05fr_0.95fr]">
          <section className="max-w-2xl space-y-6">
            <div className="inline-flex items-center gap-2 rounded-full border border-fuchsia-500/30 bg-fuchsia-500/10 px-3 py-1 text-xs font-medium text-fuchsia-200">
              <UserRoundPlus className="h-3.5 w-3.5" />
              Self-serve signup
            </div>
            <div className="space-y-4">
              <h1 className="text-4xl font-semibold tracking-tight text-white sm:text-5xl">
                Create a private workspace in a few minutes.
              </h1>
              <p className="max-w-xl text-base leading-7 text-zinc-300">
                Sign up with email, verify the account, and start with the free tier immediately.
                The backend creates a private personal organization automatically.
              </p>
            </div>
            <div className="grid gap-3 text-sm text-zinc-300 sm:grid-cols-2">
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="flex items-center gap-2 font-medium text-white">
                  <CheckCircle2 className="h-4 w-4 text-fuchsia-300" />
                  Free account
                </div>
                <p className="mt-2 leading-6">Local transcription, summaries, and session history without a subscription.</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="flex items-center gap-2 font-medium text-white">
                  <Mail className="h-4 w-4 text-fuchsia-300" />
                  Verification email
                </div>
                <p className="mt-2 leading-6">Registration sends a verification link automatically, so the login page can confirm the account.</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="flex items-center gap-2 font-medium text-white">
                  <CheckCircle2 className="h-4 w-4 text-fuchsia-300" />
                  Password policy
                </div>
                <p className="mt-2 leading-6">Minimum 8 characters with upper case, lower case, and a number.</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="flex items-center gap-2 font-medium text-white">
                  <CircleAlert className="h-4 w-4 text-fuchsia-300" />
                  Username rules
                </div>
                <p className="mt-2 leading-6">Use 3-30 characters made of letters, numbers, or underscores.</p>
              </div>
            </div>
          </section>

          <section className="rounded-3xl border border-white/10 bg-zinc-900/80 p-6 shadow-2xl shadow-black/30 backdrop-blur">
            {registrationClosed ? (
              <div className="space-y-5 rounded-2xl border border-fuchsia-500/30 bg-fuchsia-500/10 p-5 text-fuchsia-50">
                <div className="flex items-start gap-3">
                  <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0" />
                  <div>
                    <h2 className="text-xl font-semibold text-white">Thanks — we got your invite request.</h2>
                    <p className="mt-2 text-sm leading-6 text-fuchsia-100/90">
                      Meeting-Ops is in private beta. We've logged your email
                      {email.trim() ? <> (<span className="font-medium">{email.trim()}</span>)</> : null}
                      {' '}and will reach out as slots open.
                    </p>
                    <p className="mt-3 text-sm leading-6 text-fuchsia-100/90">
                      Already have an account? You can{' '}
                      <Link to="/login" className="font-medium underline-offset-4 hover:underline">
                        sign in here
                      </Link>
                      .
                    </p>
                  </div>
                </div>
                <div className="flex flex-col gap-3 sm:flex-row">
                  <Link
                    to="/pricing"
                    className="inline-flex items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm font-medium text-zinc-100 transition hover:bg-white/10"
                  >
                    View pricing
                  </Link>
                </div>
              </div>
            ) : success ? (
              <div className="space-y-5 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-5 text-emerald-100">
                <div className="flex items-start gap-3">
                  <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0" />
                  <div>
                    <h2 className="text-xl font-semibold text-white">Check your email to verify your account.</h2>
                    <p className="mt-2 text-sm leading-6 text-emerald-100/90">
                      We sent a verification link to <span className="font-medium">{email.trim()}</span>. Once it is confirmed, sign in and you will land in the app.
                    </p>
                  </div>
                </div>
                <div className="flex flex-col gap-3 sm:flex-row">
                  <Link
                    to="/login"
                    className="inline-flex items-center justify-center gap-2 rounded-xl bg-white px-4 py-3 text-sm font-semibold text-zinc-950 transition hover:bg-zinc-100"
                  >
                    <ArrowRight className="h-4 w-4" />
                    Go to sign in
                  </Link>
                  <Link
                    to="/pricing"
                    className="inline-flex items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm font-medium text-zinc-100 transition hover:bg-white/10"
                  >
                    View pricing
                  </Link>
                </div>
              </div>
            ) : (
              <>
                {wantsProIntent && (
                  <div className="mb-5 flex items-start gap-3 rounded-2xl border border-fuchsia-500/30 bg-fuchsia-500/10 px-4 py-3 text-sm text-fuchsia-100">
                    <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-fuchsia-300" />
                    <span>
                      You're on your way to <strong className="font-semibold text-white">Pro</strong>.
                      Create your free account first — once you verify your email and sign in,
                      you can complete the upgrade from{' '}
                      <span className="font-medium text-white">Settings → Plan &amp; billing</span>.
                    </span>
                  </div>
                )}
                <div className="mb-6 flex items-center justify-between">
                  <div>
                    <h2 className="text-2xl font-semibold tracking-tight text-white">Create account</h2>
                    <p className="mt-1 text-sm text-zinc-400">Register with an email address and username for the consumer backend.</p>
                  </div>
                  <UserRoundPlus className="h-5 w-5 text-fuchsia-300" />
                </div>

                {error && (
                  <div className="mb-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100" role="alert">
                    <div className="flex items-start gap-2">
                      <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                      <span>{error}</span>
                    </div>
                  </div>
                )}

                <form className="space-y-4" onSubmit={handleSubmit}>
                  <label className="block">
                    <span className="mb-2 block text-sm font-medium text-zinc-200">Full name</span>
                    <input
                      type="text"
                      autoComplete="name"
                      value={fullName}
                      onChange={(event) => setFullName(event.target.value)}
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                      placeholder="Optional"
                    />
                  </label>

                  <label className="block">
                    <span className="mb-2 block text-sm font-medium text-zinc-200">Email</span>
                    <input
                      type="email"
                      autoComplete="email"
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                      required
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                      placeholder="you@example.com"
                    />
                  </label>

                  <label className="block">
                    <span className="mb-2 block text-sm font-medium text-zinc-200">Username</span>
                    <input
                      type="text"
                      autoComplete="username"
                      value={username}
                      onChange={(event) => setUsername(event.target.value)}
                      required
                      minLength={3}
                      maxLength={30}
                      pattern="[A-Za-z0-9_]+"
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                      placeholder="username_123"
                    />
                  </label>

                  <label className="block">
                    <span className="mb-2 flex items-center gap-1.5 text-sm font-medium text-zinc-200">
                      <Ticket className="h-4 w-4 text-fuchsia-300" />
                      Invite code{requireInviteCode ? '' : ' (optional)'}
                    </span>
                    <input
                      type="text"
                      autoComplete="off"
                      value={inviteCode}
                      onChange={(event) => setInviteCode(event.target.value)}
                      required={requireInviteCode}
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                      placeholder={requireInviteCode ? 'Enter your invite code' : 'Have an invite code?'}
                    />
                    <span className="mt-2 block text-xs text-zinc-400">
                      {requireInviteCode
                        ? 'Meeting-Ops is in private beta — enter your invite code to join.'
                        : 'Have an invite code? (optional) Redeeming one upgrades your workspace to Pro.'}
                    </span>
                  </label>

                  <div className="grid gap-4 sm:grid-cols-2">
                    <label className="block">
                      <span className="mb-2 block text-sm font-medium text-zinc-200">Password</span>
                      <input
                        type="password"
                        autoComplete="new-password"
                        value={password}
                        onChange={(event) => setPassword(event.target.value)}
                        required
                        minLength={8}
                        className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                        placeholder="Minimum 8 characters"
                      />
                    </label>
                    <label className="block">
                      <span className="mb-2 block text-sm font-medium text-zinc-200">Confirm password</span>
                      <input
                        type="password"
                        autoComplete="new-password"
                        value={confirmPassword}
                        onChange={(event) => setConfirmPassword(event.target.value)}
                        required
                        minLength={8}
                        className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                        placeholder="Repeat password"
                      />
                    </label>
                  </div>

                  <label className="flex items-start gap-3 text-sm text-zinc-300">
                    <input
                      type="checkbox"
                      checked={termsAccepted}
                      onChange={(event) => setTermsAccepted(event.target.checked)}
                      required
                      className="mt-0.5 h-4 w-4 shrink-0 rounded border-white/20 bg-zinc-950 text-fuchsia-500 focus:ring-fuchsia-500"
                    />
                    <span>
                      I agree to the{' '}
                      <Link to="/terms" className="font-medium text-fuchsia-200 underline-offset-4 hover:underline">Terms</Link>,{' '}
                      <Link to="/privacy" className="font-medium text-fuchsia-200 underline-offset-4 hover:underline">Privacy Policy</Link>, and{' '}
                      <Link to="/aup" className="font-medium text-fuchsia-200 underline-offset-4 hover:underline">Acceptable Use Policy</Link>.
                    </span>
                  </label>

                  <button
                    type="submit"
                    disabled={isSubmitting || !termsAccepted}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-fuchsia-500 to-violet-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-fuchsia-500/20 transition hover:from-fuchsia-400 hover:to-violet-400 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <ArrowRight className="h-4 w-4" />
                    {isSubmitting ? 'Creating account...' : 'Create account'}
                  </button>
                </form>

                <div className="mt-5 flex items-center justify-between border-t border-white/10 pt-5 text-sm text-zinc-400">
                  <span>Already have an account?</span>
                  <Link to="/login" className="font-medium text-fuchsia-200 hover:text-fuchsia-100">
                    Sign in
                  </Link>
                </div>
              </>
            )}
          </section>
        </main>

        {/* v3.22.5 legal footer for unauthenticated marketing pages. */}
        <LegalFooter />
      </div>
    </div>
  );
};

export default Signup;
