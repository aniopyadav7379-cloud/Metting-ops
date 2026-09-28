# Developer lane: Contact-Ops and Customer-Ops enrichment

Work one task only. Create a branch from the current Meeting-Ops `main`; do not
merge it and do not deploy production.

## Goal

Make participant identity useful across the suite: Contact-Ops remains the
person system of record, and Customer-Ops can render a reliable meeting history
without copying private transcript or biometric data.

## Scope

- Resolve participant email/name to a Contact-Ops person only through the
  existing workspace-bound Brigade exchange.
- Add a manual participant search/link/unlink UI with clear match confidence
  and no automatic choice when results are ambiguous.
- Reconcile stale Contact-Ops IDs safely when contacts merge.
- Define the Customer-Ops meeting-summary contract: meeting metadata,
  participants, approved summary, decisions, and canonical action-item status.
  Raw transcript is excluded by default and requires an explicit scoped grant.
- Add pagination, stable cursors, and updated-since support to the inbound
  Customer-Ops federation surface.
- Keep speaker voice fingerprints and embeddings entirely outside Contact-Ops
  and Customer-Ops responses.

## Acceptance criteria

- Query-level tests prove workspace isolation for Contact-Ops resolution and
  Customer-Ops meeting reads.
- Invalid, expired, wrong-audience, missing-workspace, and wrong-scope tokens
  fail closed.
- Ambiguous contact matches never auto-link.
- Customer-Ops can render a customer timeline without fetching the transcript.
- Contract tests run from both producer and consumer repositories.
- A live staging read validates token exchange and pagination without mutating
  production customer records.

## Handoff

Return branches/commits for every repository touched, contract version,
changed paths, test evidence, and any Brigade allow-list gate. Stop without
merging.
