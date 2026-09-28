# Developer lane: frontend performance and dependency hardening

Work one task only. Create a branch from the current Meeting-Ops `main`; do not
merge it and do not deploy production.

## Goal

Reduce initial-load cost and dependency risk without changing recording,
transcription, or offline behavior.

## Scope

- Establish production bundle and authenticated-page performance baselines.
- Lazy-load ONNX/runtime, knowledge-graph, report, and recording-only code so
  dashboard/settings users do not download those paths up front.
- Split oversized vendor chunks with stable caching. Do not duplicate ONNX
  runtimes or model assets.
- Triage the current npm audit findings individually. Upgrade only with
  compatibility evidence; do not use an unreviewed force fix.
- Add dependency pinning and a documented update cadence for security-critical
  browser audio and model runtimes.
- Add a lightweight bundle budget to CI with an intentional override process.

## Acceptance criteria

- Record before/after compressed initial JS, largest chunk, and page-load
  timings on the same machine and network profile.
- Dashboard, session details, speaker sample playback, live recording, offline
  transcription, and report download all pass focused tests.
- No model/runtime is fetched until its owning feature is opened.
- Source maps and analytics behavior remain consistent with the existing
  deployment policy.
- The production build is warning-reviewed; every remaining audit finding has
  an owner, severity, exploitability note, and bounded follow-up.

## Handoff

Return branch, commit, changed paths, benchmark table, audit disposition, and
test/build evidence. Stop without merging.
