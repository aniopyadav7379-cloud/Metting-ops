import { useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { setOptOut } from '../../utils/posthog';

/**
 * Privacy preferences. Currently a single control: opt out of product
 * analytics (PostHog). The flag is stored in localStorage
 * (`meetingops.posthog.opt_out`) and read by `utils/posthog.ts` at init
 * + applied live via opt_out_capturing(). No Save button — the toggle
 * persists immediately (this section is in PLACEHOLDER_SECTIONS so the
 * Settings shell suppresses the backend Save row).
 */
export default function PrivacySettings() {
  const [analyticsOptOut, setAnalyticsOptOut] = useState(
    () =>
      typeof window !== 'undefined' &&
      window.localStorage.getItem('meetingops.posthog.opt_out') === '1',
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-fuchsia-500/15">
          <ShieldCheck className="h-5 w-5 text-fuchsia-300" />
        </div>
        <div>
          <h2 className="text-xl font-semibold text-white">Privacy</h2>
          <p className="text-sm text-zinc-400">
            Control how Meeting-Ops measures product usage.
          </p>
        </div>
      </div>

      <div className="rounded-lg border border-white/10 bg-zinc-900/60 p-4 sm:p-6">
        <label className="flex cursor-pointer items-start gap-3">
          <input
            type="checkbox"
            className="mt-1 h-4 w-4 shrink-0 accent-fuchsia-500"
            checked={analyticsOptOut}
            onChange={(e) => {
              setAnalyticsOptOut(e.target.checked);
              setOptOut(e.target.checked);
            }}
          />
          <span className="text-sm text-zinc-300">
            Opt out of product analytics. We use PostHog (and Umami for
            pageviews) to understand what features are useful. Browser-only —
            your audio + transcripts never go to PostHog.
          </span>
        </label>
      </div>
    </div>
  );
}
