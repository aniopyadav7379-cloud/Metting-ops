import { useRef } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Dialog } from './ui/Dialog';

/**
 * Generic confirm dialog. Previously a hand-rolled `fixed inset-0`
 * overlay; v3.19 refactor moves it on top of Radix Dialog so callers
 * inherit proper a11y (focus trap, focus return to opener,
 * `role="dialog"` + `aria-modal="true"`, ESC + click-outside).
 *
 * The public API is preserved so existing callers (AlwaysOnControl,
 * MobileLiveRecording, etc.) don't need touch-ups.
 *
 * Renders nothing when `isOpen` is false (Radix handles unmount via the
 * `open` prop, so we still gate on `isOpen` to keep the same flow).
 *
 * Default focus target on open:
 *   - Cancel button when `tone === 'danger'` (audit finding: autoFocus on
 *     the confirm button is a footgun for destructive actions; one
 *     accidental Enter delete-confirms).
 *   - Confirm button when `tone === 'primary'`.
 */

export interface ConfirmModalProps {
  isOpen: boolean;
  title: string;
  /** Body content — string or any ReactNode. Long-form ok. */
  description: React.ReactNode;
  /** "Yes, do it" button label. Defaults to 'Confirm'. */
  confirmLabel?: string;
  /** "Don't do it" button label. Defaults to 'Cancel'. */
  cancelLabel?: string;
  /**
   * Visual tone for the primary action button. 'danger' = red, 'primary'
   * = the brand fuchsia/indigo. Defaults to 'danger' since the most
   * common use is "stop other recording".
   */
  tone?: 'danger' | 'primary';
  onConfirm: () => void;
  onCancel: () => void;
  /**
   * Optional icon override. Defaults to AlertTriangle for danger tone
   * and undefined for primary.
   */
  icon?: React.ReactNode;
}

export default function ConfirmModal({
  isOpen,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  tone = 'danger',
  onConfirm,
  onCancel,
  icon,
}: ConfirmModalProps) {
  const cancelRef = useRef<HTMLButtonElement | null>(null);
  const confirmRef = useRef<HTMLButtonElement | null>(null);

  if (!isOpen) return null;

  const showIcon = icon ?? (tone === 'danger' ? (
    <AlertTriangle className="h-5 w-5 text-amber-300" />
  ) : null);

  const confirmClasses = tone === 'danger'
    ? 'bg-red-600 hover:bg-red-500 text-white'
    : 'bg-gradient-to-r from-fuchsia-600 to-indigo-600 hover:from-fuchsia-500 hover:to-indigo-500 text-white';

  return (
    <Dialog open={isOpen} onOpenChange={(open) => { if (!open) onCancel(); }}>
      <Dialog.Content
        size="md"
        describedById="confirm-modal-description"
        onOpenAutoFocus={(event) => {
          // Audit finding: destructive flows should default focus to
          // Cancel, not Confirm — one accidental Enter shouldn't fire a
          // delete. For primary-tone prompts we focus Confirm as the
          // expected positive default.
          event.preventDefault();
          if (tone === 'danger') {
            cancelRef.current?.focus();
          } else {
            confirmRef.current?.focus();
          }
        }}
      >
        <Dialog.Header>
          <div className="flex items-start gap-3">
            {showIcon}
            <Dialog.Title>{title}</Dialog.Title>
          </div>
          <Dialog.Close />
        </Dialog.Header>

        <Dialog.Description
          id="confirm-modal-description"
          className="px-5 py-4 text-sm leading-6 text-zinc-200"
          asChild
        >
          <div>{description}</div>
        </Dialog.Description>

        <Dialog.Footer>
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="rounded-lg border border-zinc-700 bg-zinc-800 px-4 py-2 text-sm font-medium text-zinc-100 transition hover:bg-zinc-700"
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            onClick={onConfirm}
            className={`rounded-lg px-4 py-2 text-sm font-semibold transition ${confirmClasses}`}
          >
            {confirmLabel}
          </button>
        </Dialog.Footer>
      </Dialog.Content>
    </Dialog>
  );
}
