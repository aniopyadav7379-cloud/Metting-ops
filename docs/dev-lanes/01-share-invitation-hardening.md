# Developer lane: harden meeting invitations

Work one task only. Create a branch from the current Meeting-Ops `main`; do not
merge it and do not deploy production.

## Goal

Make ongoing meeting access safe and understandable from invitation through
revocation. Meeting links must remain easy to use without storing reusable
invitation secrets in plaintext.

## Scope

- Replace plaintext invitation-token persistence with a one-way hash. Show a
  token only when it is created; compare hashes for later validation.
- Provide an explicit migration path for existing rows. Preserve valid legacy
  invitations during a bounded transition, then remove the legacy read path.
- Add invitation delivery state: pending, sent, failed, accepted, revoked, and
  expired. Include last attempt, failure reason safe for operators, and resend.
- Keep access sharing and “email a copy” in the same share workflow, but make
  their different outcomes explicit.
- Ensure only the meeting creator or workspace admin/manager can list,
  resend, revoke, or inspect collaborator invitations.
- Never return collaborator tokens, token hashes, provider responses, or
  private email metadata from list endpoints.

## Acceptance criteria

- Database migration and rollback are documented and exercised on a fresh DB.
- Behavior tests prove a viewer cannot list, resend, revoke, or mutate access.
- Tests prove a leaked database row cannot be used as an invitation URL.
- Resend is idempotent and rate-limited; a failed provider call does not create
  duplicate access grants.
- Revocation invalidates the invite immediately and does not remove unrelated
  workspace membership.
- The UI explains whether the recipient gets ongoing access or a static copy.
- Focused backend and frontend tests pass, and the running dev artifact is
  smoke-tested without sending a real email.

## Handoff

Return branch, commit, changed paths, migration notes, test evidence, and any
secret-rotation or live-send gate. Stop without merging.
