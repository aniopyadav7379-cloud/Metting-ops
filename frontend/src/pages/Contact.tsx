/**
 * Customer-support contact form (v3.21.0).
 *
 * Public at /contact. Renders for authed AND unauthed callers; the
 * copy + footer behavior shifts based on auth state. Posts to
 * POST /api/support/contact which is rate-limited per email (3/hr)
 * and fires a Postmark notify to SUPPORT_NOTIFY_EMAIL
 * (default support@magicunicorn.tech).
 *
 * For authed callers we pre-fill name + email from /api/auth/me via
 * AuthContext so the user doesn't have to re-type them. The endpoint
 * still validates server-side; name/email in the form body are the
 * source of truth for the email payload.
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  LifeBuoy,
  Mail,
  Sparkles,
} from 'lucide-react';
import { config } from '../config';
import { useAuth } from '../contexts/AuthContext';
import { track } from '../utils/posthog';

type SubmitState = 'idle' | 'submitting' | 'success' | 'error';

/**
 * Bucket a free-text subject line into a coarse category for analytics.
 * Keyword match only — no message body, no PII leaves the browser.
 * Falls back to 'other' when nothing matches.
 */
function deriveSubjectCategory(subject: string): 'billing' | 'support' | 'other' {
  const s = subject.toLowerCase();
  const billing = /\b(bill|billing|invoice|payment|charge|refund|subscription|subscribe|plan|pric|card|receipt|upgrade|downgrade|cancel)\w*/;
  const support = /\b(bug|error|broken|crash|issue|problem|help|support|fail|trouble|stuck|wrong|can'?t|cannot)\w*/;
  if (billing.test(s)) return 'billing';
  if (support.test(s)) return 'support';
  return 'other';
}

export const Contact: React.FC = () => {
  const { user, isAuthenticated } = useAuth();

  const initialName = useMemo(() => {
    if (!user) return '';
    return user.full_name || user.username || '';
  }, [user]);
  const initialEmail = useMemo(() => user?.email || '', [user]);

  const [name, setName] = useState(initialName);
  const [email, setEmail] = useState(initialEmail);
  const [subject, setSubject] = useState('');
  const [message, setMessage] = useState('');
  const [state, setState] = useState<SubmitState>('idle');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // If auth resolves after first render, hydrate the form once.
  useEffect(() => {
    if (initialName && !name) setName(initialName);
    if (initialEmail && !email) setEmail(initialEmail);
    // We intentionally only fire when the initial-* values change, not
    // on every keystroke — eslint-disable-next-line react-hooks/exhaustive-deps
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialName, initialEmail]);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setErrorMsg(null);

    const trimmedEmail = email.trim();
    if (!trimmedEmail || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmedEmail)) {
      setErrorMsg('Please enter a valid email address.');
      setState('error');
      return;
    }
    if (!subject.trim()) {
      setErrorMsg('Subject is required.');
      setState('error');
      return;
    }
    if (!message.trim()) {
      setErrorMsg('Message is required.');
      setState('error');
      return;
    }

    setState('submitting');
    try {
      const apiBase = config.apiBaseUrl.replace(/\/$/, '');
      const resp = await fetch(`${apiBase}/api/support/contact`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({
          name: name.trim(),
          email: trimmedEmail,
          subject: subject.trim(),
          message: message.trim(),
        }),
      });
      if (resp.status === 429) {
        setErrorMsg('Too many support requests from this email. Try again in an hour.');
        setState('error');
        return;
      }
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        const detail = (body && typeof body === 'object' && (body as { detail?: unknown }).detail) || null;
        setErrorMsg(typeof detail === 'string' ? detail : `Could not submit (HTTP ${resp.status}).`);
        setState('error');
        return;
      }
      track('contact_support_submitted', {
        subject_category: deriveSubjectCategory(subject.trim()),
      });
      setState('success');
      setSubject('');
      setMessage('');
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : 'Network error.');
      setState('error');
    }
  };

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <div className="mx-auto flex min-h-screen w-full max-w-5xl flex-col px-4 py-6 lg:px-8">
        <header className="flex items-center justify-between border-b border-white/10 pb-4">
          <Link to={isAuthenticated ? '/dashboard' : '/'} className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gradient-to-br from-fuchsia-500 to-violet-500 shadow-lg shadow-fuchsia-500/20">
              <Sparkles className="h-5 w-5 text-white" />
            </div>
            <div>
              <div className="text-sm font-semibold tracking-tight text-white">Meeting-Ops</div>
              <div className="text-xs text-zinc-400">Customer support</div>
            </div>
          </Link>
          {isAuthenticated ? (
            <Link to="/dashboard" className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white">
              Back to app
            </Link>
          ) : (
            <Link to="/login" className="rounded-full border border-white/10 px-3 py-2 text-sm text-zinc-300 transition hover:border-fuchsia-400/40 hover:text-white">
              Sign in
            </Link>
          )}
        </header>

        <main className="grid flex-1 items-start gap-10 py-10 lg:grid-cols-[1fr_1.1fr]">
          <section className="max-w-xl space-y-6">
            <div className="inline-flex items-center gap-2 rounded-full border border-fuchsia-500/30 bg-fuchsia-500/10 px-3 py-1 text-xs font-medium text-fuchsia-200">
              <LifeBuoy className="h-3.5 w-3.5" />
              Talk to a human
            </div>
            <div className="space-y-4">
              <h1 className="text-3xl font-semibold tracking-tight text-white sm:text-4xl">
                {isAuthenticated ? 'How can we help?' : 'Get in touch'}
              </h1>
              <p className="max-w-xl text-base leading-7 text-zinc-300">
                {isAuthenticated
                  ? 'Tell us what you ran into and we will reply to your account email. Sign-in info is included automatically so we can find your workspace.'
                  : 'Send us a message and we will reply to the email you leave below. For sales and enterprise inquiries this is also the right place.'}
              </p>
            </div>
            <div className="rounded-2xl border border-white/10 bg-white/5 p-4 text-sm text-zinc-300">
              <div className="flex items-center gap-2 font-medium text-white">
                <Mail className="h-4 w-4 text-fuchsia-300" />
                Prefer email?
              </div>
              <p className="mt-2 leading-6">
                You can also write to{' '}
                <a
                  className="text-fuchsia-200 underline-offset-4 hover:underline"
                  href="mailto:support@magicunicorn.tech"
                >
                  support@magicunicorn.tech
                </a>
                .
              </p>
            </div>
          </section>

          <section className="rounded-3xl border border-white/10 bg-zinc-900/80 p-6 shadow-2xl shadow-black/30 backdrop-blur">
            {state === 'success' ? (
              <div className="space-y-5 rounded-2xl border border-emerald-500/30 bg-emerald-500/10 p-5 text-emerald-100">
                <div className="flex items-start gap-3">
                  <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0" />
                  <div>
                    <h2 className="text-xl font-semibold text-white">Thanks — we'll be in touch.</h2>
                    <p className="mt-2 text-sm leading-6 text-emerald-100/90">
                      We received your message. A reply will land at{' '}
                      <span className="font-medium">{email.trim()}</span>. If it
                      doesn't show up in your inbox, please check spam.
                    </p>
                  </div>
                </div>
                <div className="flex flex-col gap-3 sm:flex-row">
                  <Link
                    to={isAuthenticated ? '/dashboard' : '/'}
                    className="inline-flex items-center justify-center gap-2 rounded-xl bg-white px-4 py-3 text-sm font-semibold text-zinc-950 transition hover:bg-zinc-100"
                  >
                    <ArrowRight className="h-4 w-4" />
                    {isAuthenticated ? 'Back to dashboard' : 'Back to home'}
                  </Link>
                  <button
                    type="button"
                    onClick={() => setState('idle')}
                    className="inline-flex items-center justify-center gap-2 rounded-xl border border-white/10 bg-white/5 px-4 py-3 text-sm font-medium text-zinc-100 transition hover:bg-white/10"
                  >
                    Send another
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="mb-6 flex items-center justify-between">
                  <div>
                    <h2 className="text-2xl font-semibold tracking-tight text-white">Contact support</h2>
                    <p className="mt-1 text-sm text-zinc-400">
                      {isAuthenticated
                        ? 'Your account email and name are pre-filled — edit if you need to.'
                        : 'Required: a working email so we can reply.'}
                    </p>
                  </div>
                  <LifeBuoy className="h-5 w-5 text-fuchsia-300" />
                </div>

                {state === 'error' && errorMsg && (
                  <div className="mb-4 rounded-2xl border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-100" role="alert">
                    <div className="flex items-start gap-2">
                      <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
                      <span>{errorMsg}</span>
                    </div>
                  </div>
                )}

                <form className="space-y-4" onSubmit={handleSubmit} noValidate>
                  <label className="block">
                    <span className="mb-2 block text-sm font-medium text-zinc-200">Name</span>
                    <input
                      type="text"
                      autoComplete="name"
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                      maxLength={200}
                      disabled={state === 'submitting'}
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60 disabled:opacity-60"
                      placeholder="Your name"
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
                      disabled={state === 'submitting'}
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60 disabled:opacity-60"
                      placeholder="you@example.com"
                    />
                  </label>

                  <label className="block">
                    <span className="mb-2 block text-sm font-medium text-zinc-200">Subject</span>
                    <input
                      type="text"
                      value={subject}
                      onChange={(event) => setSubject(event.target.value)}
                      required
                      maxLength={200}
                      disabled={state === 'submitting'}
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60 disabled:opacity-60"
                      placeholder="What's going on?"
                    />
                  </label>

                  <label className="block">
                    <span className="mb-2 block text-sm font-medium text-zinc-200">Message</span>
                    <textarea
                      value={message}
                      onChange={(event) => setMessage(event.target.value)}
                      required
                      maxLength={10000}
                      rows={7}
                      disabled={state === 'submitting'}
                      className="w-full resize-y rounded-xl border border-white/10 bg-zinc-950 px-4 py-3 text-white outline-none transition placeholder:text-zinc-500 focus:border-fuchsia-500/60 disabled:opacity-60"
                      placeholder="Tell us what you ran into, what you expected, and any error message or session ID."
                    />
                  </label>

                  <button
                    type="submit"
                    disabled={state === 'submitting'}
                    className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-fuchsia-500 to-violet-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-fuchsia-500/20 transition hover:from-fuchsia-400 hover:to-violet-400 disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    <ArrowRight className="h-4 w-4" />
                    {state === 'submitting' ? 'Sending…' : 'Send message'}
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

export default Contact;
