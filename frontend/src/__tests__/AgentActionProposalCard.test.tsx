import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

import AgentActionProposalCard from '../components/agent-actions/AgentActionProposalCard';
import type {
  AgentActionProposal,
  AgentActionProposalMessageState,
} from '../types/agent-actions.types';

function makeProposal(extra: Partial<AgentActionProposal> = {}): AgentActionProposal {
  return {
    status: 'needs_confirmation',
    action: 'rename_session',
    preview: 'Rename session',
    diff: {},
    confirmation_token: 'phc_v1_test',
    proposal_id: 'phc_v1_test',
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    ...extra,
  };
}

function makeState(extra: Partial<AgentActionProposal> = {}): AgentActionProposalMessageState {
  return { proposal: makeProposal(extra), state: 'pending' };
}

describe('AgentActionProposalCard — standard (no typed confirmation)', () => {
  it('renders a clickable Confirm immediately, calls onConfirm with the token only', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<AgentActionProposalCard state={makeState()} onConfirm={onConfirm} onCancel={onCancel} />);
    const btn = screen.getByTestId('proposal-confirm-button');
    expect(btn).not.toBeDisabled();
    expect(screen.queryByTestId('typed-confirmation-input')).toBeNull();
    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledWith('phc_v1_test', undefined);
  });
});

describe('AgentActionProposalCard — high-friction typed confirmation', () => {
  const deleteState = makeState({
    action: 'delete_session',
    preview: 'PERMANENT DELETE session #126',
    required_typed_confirmation: 'delete-126',
    confirmation_instructions: 'To confirm permanent deletion, type exactly: delete-126',
  });

  it('renders the typed-confirmation input + instructions, disables Confirm until match', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<AgentActionProposalCard state={deleteState} onConfirm={onConfirm} onCancel={onCancel} />);
    expect(screen.getByText(/To confirm permanent deletion/)).toBeInTheDocument();
    const input = screen.getByTestId('typed-confirmation-input');
    expect(input).toBeInTheDocument();
    const btn = screen.getByTestId('proposal-confirm-button');
    expect(btn).toBeDisabled();

    // Wrong value -> still disabled
    fireEvent.change(input, { target: { value: 'yes' } });
    expect(btn).toBeDisabled();

    // Correct value -> enabled
    fireEvent.change(input, { target: { value: 'delete-126' } });
    expect(btn).not.toBeDisabled();

    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledWith('phc_v1_test', 'delete-126');
  });

  it('Cancel always works regardless of typed input state', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<AgentActionProposalCard state={deleteState} onConfirm={onConfirm} onCancel={onCancel} />);
    const cancelBtn = screen.getByRole('button', { name: /cancel/i });
    expect(cancelBtn).not.toBeDisabled();
    fireEvent.click(cancelBtn);
    expect(onCancel).toHaveBeenCalledWith('phc_v1_test');
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('trims whitespace on typed input when matching + sending', () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<AgentActionProposalCard state={deleteState} onConfirm={onConfirm} onCancel={onCancel} />);
    const input = screen.getByTestId('typed-confirmation-input');
    fireEvent.change(input, { target: { value: '  delete-126  ' } });
    const btn = screen.getByTestId('proposal-confirm-button');
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledWith('phc_v1_test', 'delete-126');
  });
});
