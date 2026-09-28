import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, CheckCircle2, KeyRound, Mail, Sparkles } from 'lucide-react';
import { config } from '../config';

// Aaron's spec (v3.21.0):
//   - Email field, submit POSTs /api/auth/password/forgot.
//   - Always show the same "if an account matches…" message.
//     We never confirm or deny existence to the caller, even on a
//     transport error — that would let an attacker time the response
//     to enumerate accounts. The backend mirrors this rule.
//   - Show a generic 202-ish acknowledgement after submit; let the user
//     bounce back to /login from there.

export const ForgotPassword: React.FC = () => {
  const [email, setEmail] = useState('');
  const [submitted, setSubmitted] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setIsSubmitting(true);
    try {
      // Best-effort: 202 expected. Even on a network error we still flip
      // to the acknowledgement state — exposing the failure would leak
      // signal an attacker can use.
      await fetch(`${config.apiBaseUrl}/api/auth/password/forgot`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email: email.trim() }),
      });
    } catch {
      /* swallow — see comment above */
    } finally {
      setIsSubmitting(false);
      setSubmitted(true);
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
              <div className="text-xs text-zinc-400">Reset your password</div>
            </div>
          </Link>
          <Link to="/login" className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white">
            Back to sign in
          </Link>
        </header>

        <main className="flex flex-1 items-center justify-center py-10">
          <section className="w-full max-w-md rounded-3xl border border-white/10 bg-zinc-900/80 p-6 shadow-2xl shadow-black/30 backdrop-blur">
            {submitted ? (
              <div className="space-y-5 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-5 text-emerald-100">
                <div className="flex items-start gap-3">
                  <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0" />
                  <div>
                    <h2 className="text-xl font-semibold text-white">Check your email.</h2>
                    <p className="mt-2 text-sm leading-6 text-emerald-100/90">
                      If an account matches that email, a reset link is on the way.
                      The link expires in one hour.
                    </p>
                  </div>
                </div>
                <Link
                  to="/login"
                  className="inline-flex items-center gap-2 rounded-xl bg-white px-4 py-3 text-sm font-semibold text-zinc-950 transition hover:bg-zinc-100"
                >
                  <ArrowRight className="h-4 w-4" />
                  Return to sign in
                </Link>
              </div>
            ) : (
              <>
                <div className="mb-6 flex items-center justify-between">
                  <div>
                    <h2 className="text-2xl font-semibold tracking-tight text-white">Forgot your password?</h2>
                    <p className="mt-1 text-sm text-zinc-400">Enter the email on your account and we will send you a reset link.</p>
                  </div>
                  <KeyRound className="h-5 w-5 text-fuchsia-300" />
                </div>

                <form className="space-y-4" onSubmit={handleSubmit}>
                  <label className="block">
                    <span className="mb-2 block text-sm font-medium text-zinc-200">Email</span>
                    <div className="relative">
                      <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
                      <input
                        type="email"
                        autoComplete="email"
                        required
                        value={email}
                        onChange={(e) => setEmail(e.target.value)}
                        className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 pl-9 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60"
                        placeholder="you@example.com"
                      />
                    </div>
                  </label>

                  <button
                    type="submit"
                    disabled={isSubmitting}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-fuchsia-500 to-violet-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-fuchsia-500/20 transition hover:from-fuchsia-400 hover:to-violet-400 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <ArrowRight className="h-4 w-4" />
                    {isSubmitting ? 'Sending…' : 'Send reset link'}
                  </button>
                </form>

                <div className="mt-5 flex items-center justify-between border-t border-white/10 pt-5 text-sm text-zinc-400">
                  <span>Remembered it?</span>
                  <Link to="/login" className="font-medium text-fuchsia-200 hover:text-fuchsia-100">
                    Sign in
                  </Link>
                </div>
              </>
            )}
          </section>
        </main>
      </div>
    </div>
  );
};

export default ForgotPassword;
