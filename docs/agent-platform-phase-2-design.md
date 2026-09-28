# Phase 2 design — safe write/control MCP tools

Status: **design, awaiting ratification**. Last updated: 2026-05-27. Companion to `docs/agent-platform-roadmap.md` (Phase 2 of the agent platform arc).

## Goal
Give the agent a small, safe set of **write/control** tools it can call to act in Meeting-Ops on the user's behalf (create/rename/tag a session, trigger a reprocess, draft a follow-up email). Every mutating action goes through **propose → user confirms → mutate**, with org + tier scoping enforced at the tool boundary, an audit trail, and bounded blast radius.

This is v1 — MCP-only. The chat-panel confirmation UI is v1.1 (designed below but not built yet; the v1 surface is the MCP `confirm_action` tool so MCP-aware clients can drive it immediately).

## Hard constraints
- **No tool ever mutates on first call.** First call returns a structured "proposal" with a token + a preview of what would change. A separate `confirm_action(token)` consumes the token and mutates.
- **Tokens are scoped + short-lived.** Bound to (user_id, org_id, tool_name, payload_hash, expiry 5 min). One-shot — consuming deletes the token. Replay-protected.
- **Tier + org scoping at the tool boundary**, not just the UI. Reuse the existing `auth/tier.py` `tier_features` + `gate_feature_for_caller` pattern from v3.0.0.
- **Read-only audit trail** for every mutation (proposed_at, proposed_by, confirmed_at, confirmed_by, action, before/after, result). Reuse existing `audit_logs` table if present, else add one.
- **No destructive bulk ops in v1.** No bulk delete, no "delete all", no cross-org operations.
- **`send_email` is NOT in v1.** Only `draft_followup_email` (produces text the user copies/sends themselves). Sending email is a separate, deliberately deferred phase.

## The propose → confirm → mutate pattern (MCP shape)

Each write tool, when called, returns:

```json
{
  "status": "needs_confirmation",
  "action": "rename_session",
  "preview": "Rename session #122 \"Transcription System Review\" → \"Transcription System Review (final)\"",
  "diff": { "title": { "from": "Transcription System Review", "to": "Transcription System Review (final)" } },
  "confirmation_token": "phc_v1_<opaque>",
  "expires_at": "2026-05-27T22:35:00Z"
}
```

The agent shows the preview to the user. The user confirms. The agent calls:

```
confirm_action(token="phc_v1_<opaque>")
```

The backend validates (token exists, not expired, not consumed, scope matches caller), runs the mutation atomically, writes the audit row, deletes the token, and returns:

```json
{ "status": "applied", "action": "rename_session", "result": {...} }
```

If the user wants to cancel, the agent can call `cancel_action(token)` (or just let it expire). Cancellation is also audited.

## v1 tool list (small on purpose)

| Tool | Tier | Friction | What it does |
|---|---|---|---|
| `propose_create_session` | free+ | one-click confirm | New session row (title, optional folder/tag) |
| `propose_rename_session` | free+ | one-click confirm | Rename an owned session |
| `propose_add_tag` / `propose_remove_tag` | free+ | one-click confirm | Mutate one row's `tags` array |
| `propose_trigger_reprocess` | pro+ | one-click confirm | Re-run the server pipeline (idempotent) |
| `propose_draft_followup_email` | pro+ | one-click confirm | Returns drafted text to user (no send) |
| `confirm_action(token)` | n/a | n/a | Consume + execute |
| `cancel_action(token)` | n/a | n/a | Discard a pending proposal |

**Deliberately NOT in v1**:
- `delete_session` (destructive; needs extra friction model — typed-confirmation or cool-down. Add in v1.1 with strong confirmation UX.)
- `send_email` (deferred phase)
- `start_recording` / `stop_recording` (interacts with live state; needs more thought on multi-session/multi-room safety; v1.1)
- Anything that modifies org membership, tier, billing, or shared infra.

## Backend shape

- **`backend/services/agent_actions.py`** (new) — the propose/confirm/cancel machinery:
  - `propose(*, user, org, action, payload) -> { token, preview, diff, expires_at }`
  - `confirm(*, user, token) -> { result }` — atomic: validate → execute → audit → delete token. Transactional.
  - `cancel(*, user, token) -> ack`
  - Token storage: **Redis** (`unicorn-redis`, already wired for Arq) with TTL 300s; namespace `meeting-ops:agent-actions:`. Single-key consume via `GETDEL`. No DB table needed for tokens — they're ephemeral.
  - Audit: append to existing `audit_logs` table (check schema in `auth/models.py`); else add a small `agent_action_audit` table.
- **`backend/services/agent_write_tools.py`** (new) — the action implementations, one function per tool. Each is a thin wrapper on existing endpoints + the action layer:
  - `_check_tier_for_action(caller, action)` — gate via `tier_features`.
  - `_check_owner(session, caller)` — org/owner check.
  - Each `propose_X` returns the proposal shape; the mutation lives in `confirm` keyed by `action`.
- **`backend/api/agent_actions.py`** (new) — HTTP endpoints for the in-app chat path: `POST /api/agent-actions/propose`, `POST /api/agent-actions/confirm`, `POST /api/agent-actions/cancel`. (MCP tools call into the same service layer.)
- **`mcp/meeting_ops_mcp.py`** — add the `propose_*` + `confirm_action` + `cancel_action` MCP tools.
- **`backend/services/agent_tools.py`** — register the new tools so the agent loop knows them.

## Chat-panel UI (v1.1 — not in v1)

When we get there: reuse Crisis Ops's `IntakePlanReview.jsx` pattern — when the agent loop returns a `needs_confirmation` step, the chat panel renders a card with:
- The preview text (human-readable)
- A diff view (compact, before/after)
- **Confirm** + **Cancel** buttons (+ a 3-second cooldown on destructive actions when we add them in v1.1)
- A countdown to expiry

On confirm, the chat panel calls `POST /api/agent-actions/confirm` and renders the result inline. On cancel/expire, it dismisses with a note.

## Testing

- Unit: propose → confirm round-trip per tool (Redis mocked), expired-token rejection, replayed-token rejection, cross-org rejection, tier-gate rejection, payload-tamper rejection (the payload hash must match what was proposed).
- Integration: end-to-end against a real session — propose rename → confirm → row updated → audit row written.

## Open questions for Aaron to weigh in on

1. **MCP-only v1, chat-UI v1.1 — agreed?** Or do you want the chat-panel UI in v1 even if it pushes scope to ~1 full day?
2. **Tool list** — anything to add/remove? The instinct is to start *small* and safe; `delete_session`, `send_email`, `start_recording` are intentionally deferred. OK with that?
3. **Audit table** — reuse `audit_logs` (if it exists on the auth side) vs add a dedicated `agent_action_audit` table? I'll inspect what's there before deciding.
4. **Default tier for these tools** — free tier gets `create/rename/tag` (low risk, useful); pro+ gets `trigger_reprocess` + `draft_followup_email` (use server compute / LLM). Sound right?
5. **Token TTL** — 5 minutes is the default. Long enough for confirm UX, short enough that abandoned proposals die. Sane?

If any of those are "yes, proceed" I'll build to that; if any are "let's discuss," I'll pause that piece and ship the rest.
