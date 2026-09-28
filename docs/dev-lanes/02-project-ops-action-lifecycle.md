# Developer lane: complete the Project-Ops action-item lifecycle

Work one task only. Create a branch from the current Meeting-Ops `main`; do not
merge it and do not deploy production.

## Goal

Turn Meeting-Ops action items into a transparent propose-review-approve
workflow with Project-Ops, while keeping tenant boundaries and status ownership
unambiguous.

## Scope

- Preserve the current workspace-bound Brigade token exchange and fail-closed
  behavior. Never fall back to a default tenant.
- Add a visible Project-Ops linkage state to each action item: local only,
  proposed, approved/linked, rejected, or sync failed.
- After Project-Ops approves a proposal, persist the resulting task ID and
  canonical task URL back to the Meeting-Ops item through a workspace-scoped,
  authenticated callback or bounded reconciliation job.
- Make action-item text open its detail drawer. If linked, offer a clearly
  labeled “Open in Project-Ops” control; the checkbox continues to mean
  Meeting-Ops completion.
- Define conflict rules for status changes in both apps. Do not silently reopen
  a completed item or overwrite a newer Project-Ops state.
- Add retry visibility and an operator-safe requeue action for recoverable
  federation failures.

## Acceptance criteria

- Behavior tests prove one workspace cannot create, read, reconcile, or update
  another workspace’s proposals/tasks.
- Approval creates at most one Project-Ops task, including repeated callbacks
  and reconciliation runs.
- A local-only checkbox never implies a Project-Ops task was completed.
- Linked lifecycle refresh is idempotent and records the last successful sync;
  it never writes either app's task/completion status.
- Logs include request/correlation IDs but no bearer tokens or meeting content.
- End-to-end staging evidence covers proposal, human approval, backlink, and
  independent status refresh using a disposable test action item.

## Handoff

Return branch, commit, changed paths, API-contract notes, test evidence, and the
exact staging-only data cleanup performed. Stop without merging.
