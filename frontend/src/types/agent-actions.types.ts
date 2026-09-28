export type AgentActionName =
  | 'create_session'
  | 'rename_session'
  | 'add_tag'
  | 'remove_tag'
  | 'trigger_reprocess'
  | 'draft_followup_email'
  | 'delete_session'
  | 'start_recording'
  | 'stop_recording';

export interface AgentActionDiffEntry {
  from: unknown;
  to: unknown;
}

export interface AgentActionProposal {
  status: 'needs_confirmation';
  action: AgentActionName;
  preview: string;
  diff: Record<string, AgentActionDiffEntry>;
  confirmation_token: string;
  proposal_id: string;
  expires_at: string;
  payload?: Record<string, unknown>;
  before?: unknown;
  after?: unknown;
  /** High-friction destructive actions (e.g. delete_session) require the user
   * to echo this exact string back in `typed_confirmation` on confirm. When
   * present, the proposal card must render an input + disable Confirm until
   * the user's typed value matches exactly. */
  required_typed_confirmation?: string;
  /** Human-readable instructions describing how to confirm a high-friction
   * action — surfaced alongside the typed-confirmation input. */
  confirmation_instructions?: string;
}

export interface AgentActionResult {
  status: 'applied' | 'cancelled';
  action: AgentActionName;
  proposal_id: string;
  result?: Record<string, unknown>;
}

export interface AgentActionProposalMessageState {
  proposal: AgentActionProposal;
  state?: 'pending' | 'applied' | 'cancelled';
  result?: AgentActionResult['result'];
}

