import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { ArrowRight, CheckCircle2, CircleAlert, KeyRound, Sparkles } from 'lucide-react';
import { config } from '../config';
import { showToast } from '../components/Toast';

// Aaron's spec (v3.21.0):
//   - On mount, GET /api/auth/password/reset/validate?token=… so we
//     don't render the new-password form for a dead link.
//   - 200 → render the form. 410 → "expired" CTA back to /forgot-password.
//   - On submit, POST /api/auth/password/reset; success bounces to /login
//     with a toast.

type TokenState = 'loading' | 'valid' | 'expired' | 'invalid';

export const ResetPassword: React.FC = () => {
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') ?? '';
  const [tokenState, setTokenState] = useState<TokenState>('loading');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    async function check() {
      if (!token) {
        setTokenState('invalid');
        return;
      }
      try {
        const r = await fetch(
          `${config.apiBaseUrl}/api/auth/password/reset/validate?token=${encodeURIComponent(token)}`,
        );
        if (cancelled) return;
        if (r.status === 200) {
          setTokenState('valid');
        } else if (r.status === 410) {
          setTokenState('expired');
        } else {
          setTokenState('invalid');
        }
      } catch {
        if (!cancelled) setTokenState('invalid');
      }
    }
    check();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError('Passwords do not match.');
      return;
    }
    if (password.length < 8) {
      setError('Password must be at least 8 characters long.');
      return;
    }
    setSubmitting(true);
    try {
      const r = await fetch(`${config.apiBaseUrl}/api/auth/password/reset`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token, new_password: password }),
      });
      if (r.status === 200) {
        showToast.success('Password updated. Sign in with the new one.');
        navigate('/login?password_reset=success');
        return;
      }
      if (r.status === 410) {
        setTokenState('expired');
        return;
      }
      const body = await r.json().catch(() => null);
      const detail =
        body && typeof body.detail === 'string'
          ? body.detail
          : 'We could not reset your password. Try requesting a new link.';
      setError(detail);
    } catch {
      setError('Network error — try again in a moment.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-3xl flex-col px-4 py-6 lg:px-8">
        <header className="flex items-center justify-between border-b border-white/10 pb-4">
          <Link to="/login" className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-500 shadow-lg shadow-fuchsia-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="text-sm font-semibold tracking-tight text-white">Meeting-Ops</div>
              <div className="text-xs text-zinc-400">Choose a new password</div>
            </div>
          </Link>
          <Link to="/login" className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white">
            Sign in
          </Link>
        </header>

        <main className="flex flex-1 items-center justify-center py-10">
          <section className="w-full max-w-md rounded-3xl border border-white/10 bg-zinc-900/80 p-6 shadow-2xl shadow-black/30 backdrop-blur">
            {tokenState === 'loading' && (
              <div className="text-sm text-zinc-300">Checking your reset link…</div>
            )}

            {(tokenState === 'expired' || tokenState === 'invalid') && (
              <div className="space-y-5 rounded-2xl border border-amber-500/30 bg-amber-500/10 p-5 text-amber-100">
                <div className="flex items-start gap-3">
                  <CircleAlert className="mt-0.5 h-5 w-5 shrink-0" />
                  <div>
                    <h2 className="text-xl font-semibold text-white">
                      {tokenState === 'expired' ? 'This link expired.' : 'This link is not valid.'}
                    </h2>
                    <p className="mt-2 text-sm leading-6 text-amber-100/90">
                      Request a fresh reset link and use it within the next hour.
                    </p>
                  </div>
                </div>
                <Link
                  to="/forgot-password"
                  className="inline-flex items-center gap-2 rounded-xl bg-white px-4 py-3 text-sm font-semibold text-zinc-950 transition hover:bg-zinc-100"
                >
                  <ArrowRight className="h-4 w-4" />
                  Request a new link
                </Link>
              </div>
            )}

            {tokenState === 'valid' && (
              <>
                <div className="mb-6 flex items-center justify-between">
                  <div>
                    <h2 className="text-2xl font-semibold tracking-tight text-white">Set a new password</h2>
                    <p className="mt-1 text-sm text-zinc-400">Minimum 8 characters. Stronger is better.</p>
                  </div>
                  <KeyRound className="h-5 w-5 text-fuchsia-300" />
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
                    <span className="mb-2 block text-sm font-medium text-zinc-200">New password</span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      required
                      minLength={8}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                      placeholder="••••••••"
                    />
                  </label>

                  <label className="block">
                    <span className="mb-2 block text-sm font-medium text-zinc-200">Confirm password</span>
                    <input
                      type="password"
                      autoComplete="new-password"
                      required
                      minLength={8}
                      value={confirm}
                      onChange={(e) => setConfirm(e.target.value)}
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                      placeholder="••••••••"
                    />
                  </label>

                  <button
                    type="submit"
                    disabled={submitting}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-fuchsia-500 to-violet-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-fuchsia-500/20 transition hover:from-fuchsia-400 hover:to-violet-400 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <CheckCircle2 className="h-4 w-4" />
                    {submitting ? 'Updating…' : 'Update password'}
                  </button>
                </form>
              </>
            )}
          </section>
        </main>
      </div>
    </div>
  );
};

export default ResetPassword;
