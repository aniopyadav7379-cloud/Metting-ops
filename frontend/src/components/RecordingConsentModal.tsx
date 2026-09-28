import { useEffect, useRef, useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { Dialog } from './ui/Dialog';
import {
  RECORDING_CONSENT_TITLE,
  RECORDING_CONSENT_BODY,
  RECORDING_CONSENT_CHECKBOX_LABEL,
} from '../constants/legal';

interface RecordingConsentModalProps {
  open: boolean;
  /** Fired when the user ticks the required box and confirms. */
  onConfirm: () => void;
  /** Fired on cancel / dismiss (Esc, outside-click, Cancel, X). */
  onCancel: () => void;
}

/**
 * Pre-record consent gate (AUP §2.3). Shown before recording starts. The
 * confirm button stays disabled until the required checkbox is ticked. All
 * copy lives in ../constants/legal (COUNSEL-REVIEW) so counsel can edit it in
 * one place. Consent persistence is handled by the caller (see
 * hasRecordingConsent / rememberRecordingConsent).
 */
export default function RecordingConsentModal({ open, onConfirm, onCancel }: RecordingConsentModalProps) {
  const [checked, setChecked] = useState(false);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  // Reset the checkbox each time the modal opens so consent is always a
  // deliberate per-open action, never carried over from a prior open.
  useEffect(() => {
    if (open) setChecked(false);
  }, [open]);

  if (!open) return null;

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onCancel(); }}>
      <Dialog.Content
        size="md"
        describedById="recording-consent-description"
        onOpenAutoFocus={(event) => {
          // Default focus to Cancel: consent must be an intentional tick +
          // confirm, never an accidental Enter on open.
          event.preventDefault();
          cancelRef.current?.focus();
        }}
      >
        <Dialog.Header>
          <div className="flex items-start gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-fuchsia-500/15 text-fuchsia-300">
              <ShieldCheck className="h-4 w-4" />
            </div>
            <Dialog.Title>{RECORDING_CONSENT_TITLE}</Dialog.Title>
          </div>
          <Dialog.Close />
        </Dialog.Header>

        <Dialog.Description
          id="recording-consent-description"
          className="px-5 pt-4 text-sm leading-6 text-zinc-200"
        >
          {RECORDING_CONSENT_BODY}
        </Dialog.Description>

        <div className="px-5 pb-4 pt-3">
          <label className="flex items-start gap-3 rounded-lg border border-zinc-800 bg-black/20 px-3 py-3 text-sm text-zinc-100">
            <input
              type="checkbox"
              checked={checked}
              onChange={(event) => setChecked(event.target.checked)}
              className="mt-0.5 h-4 w-4 shrink-0 rounded border-zinc-600 bg-zinc-900 text-fuchsia-500 focus:ring-fuchsia-500"
            />
            <span>{RECORDING_CONSENT_CHECKBOX_LABEL}</span>
          </label>
        </div>

        <Dialog.Footer>
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-sm font-medium text-zinc-100 transition hover:bg-zinc-700"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!checked}
            onClick={() => { if (checked) onConfirm(); }}
            className="rounded-lg bg-gradient-to-r from-fuchsia-600 to-indigo-600 px-4 py-2 text-sm font-semibold text-white transition hover:from-fuchsia-500 hover:to-indigo-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            Start recording
          </button>
        </Dialog.Footer>
      </Dialog.Content>
    </Dialog>
  );
}
