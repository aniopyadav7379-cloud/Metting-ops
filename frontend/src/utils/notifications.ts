import { createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { showToast } from '../components/Toast';
import ConfirmModal from '../components/ConfirmModal';
import { getErrorMessage } from './errorHandling';

/**
 * Display a notification to the user via the shared toast system (react-toastify).
 */
export function showNotification(message: string, type: 'success' | 'error' | 'info' = 'info'): void {
  showToast[type](message);
}

/**
 * Display an error notification with proper error handling
 */
export function showError(error: unknown, context?: string): void {
  const message = getErrorMessage(error);
  const fullMessage = context ? `${context}: ${message}` : message;
  showNotification(fullMessage, 'error');
}

/**
 * Display a success notification
 */
export function showSuccess(message: string): void {
  showNotification(message, 'success');
}

/**
 * Display an info notification
 */
export function showInfo(message: string): void {
  showNotification(message, 'info');
}

export interface ConfirmOptions {
  /** Dialog heading. Defaults to 'Are you sure?'. */
  title?: string;
  /** Confirm button label. Defaults to 'Confirm'. */
  confirmLabel?: string;
  /** Cancel button label. Defaults to 'Cancel'. */
  cancelLabel?: string;
  /**
   * Visual tone of the confirm button. Defaults to 'danger', which also
   * default-focuses Cancel (ConfirmModal behavior) — matching the
   * default-cancel safety of the destructive flows this replaces.
   */
  tone?: 'danger' | 'primary';
}

/**
 * Promise-based styled confirm — the in-app replacement for native
 * `window.confirm()`. Renders the shared <ConfirmModal> (Radix Dialog:
 * focus trap, ESC, click-outside) into a throwaway root and resolves
 * `true` on confirm, `false` on cancel/dismiss.
 *
 *   if (!(await showConfirm('Delete this thing? This cannot be undone.'))) return;
 */
export function showConfirm(message: string, options: ConfirmOptions = {}): Promise<boolean> {
  return new Promise((resolve) => {
    const host = document.createElement('div');
    document.body.appendChild(host);
    const root = createRoot(host);
    let settled = false;
    const finish = (result: boolean) => {
      if (settled) return;
      settled = true;
      resolve(result);
      // Tear down on the next tick so the resolving click/keydown event
      // finishes dispatching before React unmounts the tree.
      setTimeout(() => {
        root.unmount();
        host.remove();
      }, 0);
    };
    root.render(
      createElement(ConfirmModal, {
        isOpen: true,
        title: options.title ?? 'Are you sure?',
        // whitespace-pre-line: several migrated call sites used \n in
        // their native confirm() copy.
        description: createElement('span', { className: 'whitespace-pre-line' }, message),
        confirmLabel: options.confirmLabel,
        cancelLabel: options.cancelLabel,
        tone: options.tone ?? 'danger',
        onConfirm: () => finish(true),
        onCancel: () => finish(false),
      }),
    );
  });
}
