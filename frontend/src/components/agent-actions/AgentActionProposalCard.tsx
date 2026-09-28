import React, { useEffect, useState } from 'react';
import { AlertTriangle, Check, Clock3, Loader2, X } from 'lucide-react';

import AgentActionDiff from './AgentActionDiff';
import type { AgentActionProposalMessageState } from '../../types/agent-actions.types';

interface Props {
  state: AgentActionProposalMessageState;
  busy?: boolean;
  onConfirm: (token: string, typedConfirmation?: string) => void | Promise<void>;
  onCancel: (token: string) => void | Promise<void>;
}

export default function AgentActionProposalCard({ state, busy = false, onConfirm, onCancel }: Props) {
  const proposal = state.proposal;
  const proposalState = state.state || 'pending';
  const isPending = proposalState === 'pending';
  const statusLabel =
    proposalState === 'applied'
      ? 'Applied'
      : proposalState === 'cancelled'
        ? 'Cancelled'
        : 'Awaiting confirmation';

  // High-friction destructive actions ship a `required_typed_confirmation`;
  // the user must echo it exactly before Confirm becomes clickable.
  const requiredTyped = proposal.required_typed_confirmation;
  const requiresTyped = Boolean(requiredTyped);
  const [typedInput, setTypedInput] = useState('');
  const typedMatches = !requiresTyped || typedInput.trim() === requiredTyped;

  // Reset the typed value if the user is shown a new proposal (token changes).
  useEffect(() => {
    setTypedInput('');
  }, [proposal.confirmation_token]);

  const accent = requiresTyped
    ? 'border-rose-500/40 bg-rose-950/40 shadow-rose-950/20'
    : 'border-fuchsia-500/30 bg-zinc-950/80 shadow-fuchsia-950/20';
  const iconBg = requiresTyped
    ? 'bg-rose-500/15 text-rose-300'
    : 'bg-fuchsia-500/15 text-fuchsia-300';

  return (
    <div className={`mt-3 rounded-xl border p-4 shadow-lg ${accent}`}>
      <div className="flex items-start gap-3">
        <div className={`mt-0.5 flex h-8 w-8 items-center justify-center rounded-full ${iconBg}`}>
          {requiresTyped ? <AlertTriangle className="h-4 w-4" /> : <Clock3 className="h-4 w-4" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-sm font-medium text-zinc-100">
              {requiresTyped ? 'Destructive action proposal' : 'Action proposal'}
            </div>
            <span className="rounded-full border border-zinc-700 px-2 py-0.5 text-[11px] uppercase tracking-wide text-zinc-300">
              {proposal.action}
            </span>
            <span
              className={`rounded-full px-2 py-0.5 text-[11px] uppercase tracking-wide ${
                proposalState === 'applied'
                  ? 'bg-emerald-500/15 text-emerald-300'
                  : proposalState === 'cancelled'
                    ? 'bg-zinc-800 text-zinc-400'
                    : requiresTyped
                      ? 'bg-rose-500/15 text-rose-300'
                      : 'bg-amber-500/15 text-amber-300'
              }`}
            >
              {statusLabel}
            </span>
          </div>
          <p className="mt-1 text-sm text-zinc-200">{proposal.preview}</p>
          <p className="mt-1 text-[11px] text-zinc-500">
            Expires at {new Date(proposal.expires_at).toLocaleString()}
          </p>
        </div>
      </div>

      <AgentActionDiff diff={proposal.diff} />

      {isPending && requiresTyped && (
        <div className="mt-4 rounded-lg border border-rose-500/30 bg-rose-950/30 p-3">
          {proposal.confirmation_instructions && (
            <p className="text-xs text-rose-200">{proposal.confirmation_instructions}</p>
          )}
          <label className="mt-2 block">
            <span className="sr-only">Type confirmation phrase</span>
            <input
              type="text"
              value={typedInput}
              onChange={(e) => setTypedInput(e.target.value)}
              placeholder={requiredTyped}
              autoComplete="off"
              spellCheck={false}
              disabled={busy}
              data-testid="typed-confirmation-input"
              className="w-full rounded-md border border-rose-500/40 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-rose-400 focus:outline-none focus:ring-1 focus:ring-rose-500 disabled:opacity-50"
            />
          </label>
          <p className="mt-1 text-[11px] text-zinc-500">
            Confirm stays disabled until you type the exact phrase above.
          </p>
        </div>
      )}

      {proposalState === 'applied' && state.result && (
        <div className="mt-3 rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-3 text-sm text-emerald-100">
          Applied successfully.
          <div className="mt-1 text-xs text-emerald-200/80">
            {JSON.stringify(state.result)}
          </div>
        </div>
      )}

      {proposalState === 'cancelled' && (
        <div className="mt-3 rounded-lg border border-zinc-700 bg-zinc-900 p-3 text-sm text-zinc-300">
          Proposal cancelled.
        </div>
      )}

      {isPending && (
        <div className="mt-4 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() =>
              onConfirm(
                proposal.confirmation_token,
                requiresTyped ? typedInput.trim() : undefined,
              )
            }
            disabled={busy || !typedMatches}
            data-testid="proposal-confirm-button"
            className={`inline-flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-white transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
              requiresTyped
                ? 'bg-rose-600 hover:bg-rose-500'
                : 'bg-emerald-600 hover:bg-emerald-500'
            }`}
          >
            {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
            {requiresTyped ? 'Confirm permanent deletion' : 'Confirm'}
          </button>
          <button
            type="button"
            onClick={() => onCancel(proposal.confirmation_token)}
            disabled={busy}
            className="inline-flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm font-medium text-zinc-200 transition-colors hover:border-zinc-500 hover:bg-zinc-800 disabled:cursor-not-allowed disabled:opacity-50"
          >
            <X className="h-4 w-4" />
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}
