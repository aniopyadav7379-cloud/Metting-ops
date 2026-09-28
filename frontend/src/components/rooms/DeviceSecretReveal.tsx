import { useState } from 'react';
import { AlertTriangle, Copy, Check, Eye, EyeOff, X } from 'lucide-react';

interface DeviceSecretRevealProps {
  /** The plaintext device_secret returned by /api/rooms/pairing-codes/redeem. */
  secret: string;
  /** The device_id the secret was issued for. */
  deviceId: string;
  /** Optional warning string from the API (or a sensible default). */
  warning?: string | null;
  /** Called when the operator dismisses the panel. After dismiss the
   * secret is gone — there is no way to surface it again. */
  onDismiss: () => void;
}

/**
 * One-time reveal of a freshly-issued satellite device_secret.
 *
 * The secret is returned by the redemption endpoint exactly once. The
 * server never stores plaintext and there is no recovery path — if the
 * operator closes this panel without copying the secret, the device must
 * re-pair. The component:
 *
 *   * starts with the secret masked (eye-toggle to reveal)
 *   * exposes a copy-to-clipboard button with a "Copied!" indicator
 *   * shows the warning prominently
 *   * never logs the secret to console (the value lives only in
 *     component state and clipboard buffer)
 *
 * NOTE: real satellites will usually redeem from their own firmware/agent
 * and never surface a UI here. This component exists for the ops case
 * where an admin pairs a device manually from a laptop.
 */
export default function DeviceSecretReveal({
  secret,
  deviceId,
  warning,
  onDismiss,
}: DeviceSecretRevealProps) {
  const [revealed, setRevealed] = useState(false);
  const [copied, setCopied] = useState(false);

  const masked = '•'.repeat(Math.min(secret.length, 48));

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      /* clipboard failure is non-fatal; operator can read+type */
    }
  };

  const message =
    warning ??
    'Save this device_secret securely. It is shown exactly once and cannot be retrieved later.';

  return (
    <div
      role="alertdialog"
      aria-labelledby="device-secret-title"
      className="rounded-2xl border border-amber-500/40 bg-amber-950/30 p-5 shadow-lg"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-2">
          <AlertTriangle className="h-5 w-5 mt-0.5 text-amber-300" />
          <div>
            <div id="device-secret-title" className="text-sm font-semibold text-amber-100">
              Device secret issued — one-time view
            </div>
            <div className="mt-1 text-xs text-amber-200/80">
              {message}
            </div>
          </div>
        </div>
        <button
          type="button"
          onClick={onDismiss}
          className="rounded-md p-1 text-amber-200/70 hover:bg-amber-900/40 hover:text-amber-100"
          aria-label="Dismiss"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="mt-4 grid grid-cols-1 gap-1 text-xs text-amber-100">
        <div>
          <span className="text-amber-300/70">device_id:</span>{' '}
          <span className="font-mono">{deviceId}</span>
        </div>
        <div className="mt-2 flex items-center gap-2">
          <code
            className="flex-1 rounded-md bg-zinc-950/70 px-3 py-2 font-mono text-sm text-amber-50 break-all"
            data-testid="device-secret-value"
          >
            {revealed ? secret : masked}
          </code>
          <button
            type="button"
            onClick={() => setRevealed((v) => !v)}
            className="rounded-md border border-amber-600/50 bg-zinc-950/40 px-2 py-2 text-xs text-amber-100 hover:bg-amber-900/40"
            aria-label={revealed ? 'Hide secret' : 'Show secret'}
          >
            {revealed ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
          <button
            type="button"
            onClick={copy}
            className="inline-flex items-center gap-1 rounded-md border border-amber-600/60 bg-amber-900/40 px-3 py-2 text-xs font-medium text-amber-50 hover:bg-amber-800/60"
          >
            {copied ? <Check className="h-3.5 w-3.5 text-emerald-300" /> : <Copy className="h-3.5 w-3.5" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        </div>
      </div>

      <div className="mt-3 text-xs text-amber-200/70">
        Closing this panel destroys the local copy. If lost, the device
        must re-pair to receive a new secret.
      </div>
    </div>
  );
}
