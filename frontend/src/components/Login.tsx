import React, { useEffect, useMemo, useState } from 'react';
import { Navigate, Link, useLocation, useNavigate } from 'react-router-dom';
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  Mail,
  LockKeyhole,
  LogIn,
  ShieldCheck,
  Sparkles,
  Users,
} from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { config } from '../config';
import { redirectToLogin } from '../utils/loginRedirect';
import LegalFooter from './LegalFooter';

type VerifyState = 'success' | 'invalid' | null;

export const Login: React.FC = () => {
  const { login, isAuthenticated, isLoading } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  const verifyStatus = useMemo<VerifyState>(() => {
    const value = new URLSearchParams(location.search).get('verify');
    if (value === 'success' || value === 'invalid') return value;
    return null;
  }, [location.search]);

  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [resendEmail, setResendEmail] = useState('');
  const [resendState, setResendState] = useState<'idle' | 'sending' | 'sent' | 'error'>('idle');
  const [resendMessage, setResendMessage] = useState<string | null>(null);

  useEffect(() => {
    if (identifier.includes('@')) {
      setResendEmail(identifier.trim());
    }
  }, [identifier]);

  useEffect(() => {
    if (verifyStatus === 'success') {
      setResendMessage(null);
    }
  }, [verifyStatus]);

  if (!isLoading && isAuthenticated) {
    return <Navigate to="/record" replace />;
  }

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitError(null);
    setIsSubmitting(true);

    try {
      await login(identifier.trim(), password);
      navigate('/dashboard', { replace: true });
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : 'Sign in failed');
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleResendVerification = async () => {
    const email = resendEmail.trim();
    if (!email) {
      setResendState('error');
      setResendMessage('Enter the email address used to create the account.');
      return;
    }

    setResendState('sending');
    setResendMessage(null);

    try {
      const response = await fetch(`${config.apiUrl}/api/auth/resend-verification`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify({ email }),
      });

      if (!response.ok) {
        throw new Error('Unable to resend verification email right now.');
      }

      setResendState('sent');
      setResendMessage('If that email is registered, a new verification link has been sent.');
    } catch (error) {
      setResendState('error');
      setResendMessage(error instanceof Error ? error.message : 'Unable to resend verification email right now.');
    }
  };

  const verifyBanner = verifyStatus === 'success'
    ? {
        tone: 'success' as const,
        title: 'Email verified, please sign in.',
        body: 'Your account is ready. Use the form below to continue.',
      }
    : verifyStatus === 'invalid'
      ? {
          tone: 'warning' as const,
          title: 'That verification link was invalid or expired.',
          body: 'Request a fresh link using the email you signed up with.',
        }
      : null;

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-6xl flex-col px-4 py-6 lg:px-8">
        <header className="flex items-center justify-between border-b border-white/10 pb-4">
          <Link to="/pricing" className="flex items-center gap-3 text-left">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-500 shadow-lg shadow-fuchsia-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="text-sm font-semibold tracking-tight text-white">Meeting-Ops</div>
              <div className="text-xs text-zinc-400">Consumer sign-in</div>
            </div>
          </Link>
          <div className="flex items-center gap-2">
            <Link
              to="/pricing"
              className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white"
            >
              Pricing
            </Link>
            <Link
              to="/signup"
              className="inline-flex items-center gap-2 rounded-full bg-white/5 px-3 py-2 text-sm text-zinc-100 transition hover:bg-white/10"
            >
              <ArrowRight className="h-4 w-4" />
              Sign up
            </Link>
          </div>
        </header>

        <main className="grid flex-1 items-center gap-10 py-10 lg:grid-cols-[1.1fr_0.9fr]">
          <section className="max-w-2xl space-y-6">
            <div className="inline-flex items-center gap-2 rounded-full border border-fuchsia-500/30 bg-fuchsia-500/10 px-3 py-1 text-xs font-medium text-fuchsia-200">
              <ShieldCheck className="h-3.5 w-3.5" />
              Private transcription for consumers and teams
            </div>
            <div className="space-y-4">
              <h1 className="text-4xl font-semibold tracking-tight text-white sm:text-5xl">
                Sign in to meeting transcription that keeps the flow moving.
              </h1>
              <p className="max-w-xl text-base leading-7 text-zinc-300">
                Use your email or username for a consumer account, or continue with enterprise SSO.
                The same workspace supports local capture, cloud sync, and the higher-tier features when you need them.
              </p>
            </div>
            <div className="grid gap-3 text-sm text-zinc-300 sm:grid-cols-2">
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="flex items-center gap-2 font-medium text-white">
                  <CheckCircle2 className="h-4 w-4 text-fuchsia-300" />
                  Consumer account
                </div>
                <p className="mt-2 leading-6">Self-serve signup creates a private personal org and sends a verification email automatically.</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="flex items-center gap-2 font-medium text-white">
                  <Users className="h-4 w-4 text-fuchsia-300" />
                  Enterprise SSO
                </div>
                <p className="mt-2 leading-6">Keycloak via oauth2-proxy stays available for managed orgs and existing users.</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="flex items-center gap-2 font-medium text-white">
                  <LockKeyhole className="h-4 w-4 text-fuchsia-300" />
                  Private by default
                </div>
                <p className="mt-2 leading-6">Free tier keeps audio on device and preserves local session history.</p>
              </div>
              <div className="rounded-2xl border border-white/10 bg-white/5 p-4">
                <div className="flex items-center gap-2 font-medium text-white">
                  <Mail className="h-4 w-4 text-fuchsia-300" />
                  Verification first
                </div>
                <p className="mt-2 leading-6">Verify your email once, then sign in from any browser session.</p>
              </div>
            </div>
          </section>

          <section className="rounded-3xl border border-white/10 bg-zinc-900/80 p-6 shadow-2xl shadow-black/30 backdrop-blur">
            {verifyBanner && (
              <div
                className={`mb-5 rounded-2xl border px-4 py-3 text-sm ${
                  verifyBanner.tone === 'success'
                    ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-100'
                    : 'border-amber-500/30 bg-amber-500/10 text-amber-100'
                }`}
                role="status"
              >
                <div className="font-medium">{verifyBanner.title}</div>
                <div className="mt-1 text-sm text-current/90">{verifyBanner.body}</div>
                {verifyStatus === 'invalid' && (
                  <div className="mt-3 flex flex-col gap-3 sm:flex-row">
                    <input
                      type="email"
                      value={resendEmail}
                      onChange={(event) => setResendEmail(event.target.value)}
                      placeholder="Email address"
                      className="min-w-0 flex-1 rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 text-sm text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                    />
                    <button
                      type="button"
                      onClick={handleResendVerification}
                      disabled={resendState === 'sending'}
                      className="inline-flex items-center justify-center gap-2 rounded-xl bg-white px-4 py-2 text-sm font-medium text-zinc-950 transition hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      <Mail className="h-4 w-4" />
                      {resendState === 'sending' ? 'Sending...' : 'Resend'}
                    </button>
                  </div>
                )}
                {resendMessage && (
                  <div className={`mt-2 text-xs ${resendState === 'error' ? 'text-rose-100' : 'text-current/80'}`}>
                    {resendMessage}
                  </div>
                )}
              </div>
            )}

            <div className="mb-6 flex items-center justify-between">
              <div>
                <h2 className="text-2xl font-semibold tracking-tight text-white">Sign in</h2>
                <p className="mt-1 text-sm text-zinc-400">Use your email or username with the password you set during signup.</p>
              </div>
              <LogIn className="h-5 w-5 text-fuchsia-300" />
            </div>

            {submitError && (
              <div className="mb-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100" role="alert">
                <div className="flex items-start gap-2">
                  <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{submitError}</span>
                </div>
              </div>
            )}

            <form className="space-y-4" onSubmit={handleSubmit}>
              <label className="block">
                <span className="mb-2 block text-sm font-medium text-zinc-200">Email or username</span>
                <input
                  type="text"
                  autoComplete="username"
                  value={identifier}
                  onChange={(event) => setIdentifier(event.target.value)}
                  required
                  className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                  placeholder="you@example.com or username"
                />
              </label>

              <label className="block">
                <span className="mb-2 block text-sm font-medium text-zinc-200">Password</span>
                <input
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                  className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                  placeholder="••••••••"
                />
              </label>

              <button
                type="submit"
                disabled={isSubmitting}
                className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-fuchsia-500 to-violet-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-fuchsia-500/20 transition hover:from-fuchsia-400 hover:to-violet-400 disabled:cursor-not-allowed disabled:opacity-60"
              >
                <ArrowRight className="h-4 w-4" />
                {isSubmitting ? 'Signing in...' : 'Sign in'}
              </button>

              <div className="flex justify-end pt-1 text-xs">
                <Link
                  to="/forgot-password"
                  className="text-zinc-400 hover:text-fuchsia-200"
                >
                  Forgot your password?
                </Link>
              </div>
            </form>

            <div className="mt-5 space-y-3 border-t border-white/10 pt-5">
              <button
                type="button"
                onClick={() => redirectToLogin('/dashboard')}
                className="inline-flex w-full items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm font-medium text-zinc-100 transition hover:border-fuchsia-400/40 hover:bg-white/10"
              >
                <ShieldCheck className="h-4 w-4 text-fuchsia-300" />
                Sign in with Unicorn Commander
              </button>
              <div className="flex items-center justify-between text-sm text-zinc-400">
                <span>Need a consumer account?</span>
                <Link to="/signup" className="font-medium text-fuchsia-200 hover:text-fuchsia-100">
                  Create one
                </Link>
              </div>
            </div>
          </section>
        </main>

        {/* v3.22.5 legal footer for unauthenticated marketing pages. */}
        <LegalFooter />
      </div>
    </div>
  );
};

export default Login;
