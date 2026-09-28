# UC-Meeting-Ops Design Documents

Design and architecture documents for UC-Meeting-Ops.

Last updated: 2026-07-17.

This directory contains design notes, architecture decisions, operational
references, and roadmap documents. Status labels describe the document's
relationship to the product as of the last updated date.

## Meeting & Lecture Intelligence (Omi + Qdrant + Lyzr)

- [`architecture.md`](architecture.md). Status: live. The voice-to-insight pipeline built on top of the core recorder: Omi ingestion, Qdrant as the single semantic memory for meetings/lectures/documents, Lyzr as the selectable reasoning engine, and why each sits where it does.
- [`api.md`](api.md). Status: live. Endpoint reference for lectures, documents, decisions/risks, important moments, Google Calendar, and the 8 task/notification integrations.
- [`deployment.md`](deployment.md). Status: live. Local setup, the create_all_tables.py vs. Alembic bootstrap distinction, Docker, and environment variables.
- [`security.md`](security.md). Status: live. Tenant isolation, auth, secret handling, and AI-output grounding for the additions above.
- [`integrations.md`](integrations.md). Status: live. Setup steps and IMPLEMENTED/CONTRACT TESTED/CONFIGURED/LIVE VERIFIED status for all 9 productivity integrations.
- [`omi-integration.md`](omi-integration.md). Status: live. Omi's webhook auth model and the Lyzr Agent API integration, including the investigated-and-rejected Lyzr tool-calling alternative.

## Architecture and economics

- [`compute-economics.md`](compute-economics.md) (294 lines). Status: live. Browser-first compute model, unit-economics moat at 10-100x vs Otter, Granola, Fathom, and Fireflies, proposed Free, Pro, and Enterprise tiers, and deployment topology from `magicunicorn.dev` to `unicorncommander.ai` to enterprise. Reflects shipped v0.7.4, v0.8.0, and v0.8.1 architecture.
- [`browser-models.md`](browser-models.md) (163 lines). Status: live. Operational companion to `compute-economics.md`: what the browser actually downloads for live capture (Parakeet-TDT 0.6B INT8 STT, Qwen 3 0.6B / Gemma 4 E2B rolling summary), how each is cached (CacheStorage / IndexedDB), the device gating that drops weak devices to capture-only mode, and how to recover a failed first-run download. Reflects the shipped `inBrowserSTT.ts` / `inBrowserLLM.ts` / `deviceDetection.ts` stack.
- [`local-only-mode.md`](local-only-mode.md) (66 lines). Status: live (v3.57.x). The privacy-mode pipeline: the two-stage stop flow (live-slice roll-up floor + the chunked full-quality Parakeet pass via `transcribeLong()` — ~5-min silence-aligned windows, yielding mel loop, live progress), the `shouldRunFullLocalPass` device gates and what they actually protect (decode memory, not compute), and the IndexedDB storage model.

## Testing and QA

- [`manual-test-plan.md`](manual-test-plan.md) (160 lines). Status: live (v3.57.x). Human walkthrough covering the ten customer-visible surfaces: landing page (rotation, watermark, lightbox), Google signup (the CSRF-window fix), server-mode recording E2E, the session record (audio playback, exports, quick actions, TTS label check), speakers (re-detect + naming), local-only mode incl. the chunked pass staying responsive, cross-org sharing incl. the Knowledge Graph tab, session self-heal, iPhone Safari + native app (incl. the lock-screen suspend check), and the Aaron-only $1 real-card billing gate. Includes a results table and a known-fine list.

## Inference gateway, metering, and pipeline

- [`inference-gateway-and-uc-rollout-2026-06-07.md`](inference-gateway-and-uc-rollout-2026-06-07.md) (47 lines). Status: parked plan (not built). Route all inference through `unicorncommander.ai` as one gateway — local-first (the bigboy GPU cluster) with 3rd-party fallback — so remote customer nodes hit one reachable endpoint instead of each GPU service directly. Includes the per-service fallback build order and the customer-node reachability blocker it unblocks.
- [`inference-metering-credits-plan-2026-06-06.md`](inference-metering-credits-plan-2026-06-06.md) (90 lines). Status: design brief (nothing built). UC-suite inference metering plus Unicorn Credits: four cost tiers (T0 browser / T1 light-GPU capped / T2 heavy-or-external metered / T3 BYOK) decided once at a single `MeteredProvider` chokepoint, debiting credits for T2 only, on success, exactly once — so the browser-first $0 moat is enforced in the billing layer. Meeting-Ops claims are file:line-verified.
- [`inference-pipeline-2026-07-02-concurrency-and-24k-tuning.md`](inference-pipeline-2026-07-02-concurrency-and-24k-tuning.md) (79 lines). Status: shipped 2026-07-02. The server-side reprocess pipeline stages and GPU routing (Parakeet 1.1B on 4070/midboy1, pyannote 3.1 on the P40/midboy2), plus two changes: running diarization concurrently with transcription via `asyncio.create_task` before the STT await (wall-clock drops to ~max(STT, diarize)), and the summary chunk/geometry tuning for the 24k context slot.
- [`throughput-benchmark-2026-06-08.md`](throughput-benchmark-2026-06-08.md) (67 lines). Status: measured benchmark. Single-stream throughput for the server completion pass — Parakeet 1.1B STT (~60x realtime, ~120 meetings/hour), pyannote 3.1 diarization, and a separate LLM tok/s figure — all measured on a warm, clean single-speaker ~263 s clip, not modeled, and framed as a once-per-finished-meeting batch cost rather than a per-user-hour cost.
- [`launch-metering-finding-2026-06-24.md`](launch-metering-finding-2026-06-24.md) (70 lines). Status: verification finding. Launch-handshake item (6): the app code resolves the summary provider per-org (`registry.get_llm(org_id, …)`), but the live env's `_direct_summarizer_provider` route wins by default and bypasses the metered gateway, so the server-pass summary is not currently metered per-org — a deploy-config / federation-key issue, not an app-code bug.

## Storage, protocols, and capture devices

- [`audio-storage-garage.md`](audio-storage-garage.md). Status: live (cutover complete, v3.9.0). Canonical meeting audio in the Garage `meeting-ops-audio` bucket: the `media_storage` / `session_media` modules, the `audio_storage_backend` / `audio_object_key` columns (alembic 031), read/write/delete flow, the backfill + eviction scripts, bucket provisioning, and the Garage HEAD-400 gotcha.
- [`POSTGRESQL_QDRANT_SETUP.md`](POSTGRESQL_QDRANT_SETUP.md) (333 lines). Status: live guide. PostgreSQL and Qdrant setup, SQLite migration, semantic search endpoints, and vector-search operating notes.
- [`WYOMING_PROTOCOL.md`](WYOMING_PROTOCOL.md) (336 lines). Status: designed reference. Wyoming Protocol integration for Home Assistant satellite microphones, wake words, gesture controls, and remote activation devices.
- [`always-on-recording-design.md`](always-on-recording-design.md) (183 lines). Status: designed, partly superseded by browser always-on work. Continuous recording and silence-gap segmentation design for server-side always-on capture.
- [`companion-app-design.md`](companion-app-design.md) (396 lines). Status: designed. Lightweight desktop companion app for microphone and system-audio capture, with macOS Swift native as the primary path and Electron as a cross-platform fallback.
- [`satellite-devices-design.md`](satellite-devices-design.md) (379 lines). Status: designed, with supporting backend infrastructure already present. ESP32-S3 and Raspberry Pi satellite-device architecture for multi-room recording, store-and-forward upload, and optional local STT.

## Mobile roadmap (A → B → C-1 → C-2 → C-3)

Phased plan to bring Meeting-Ops to mobile, watch, and native devices. Phase A complete in v0.8.0 and v0.8.1. Subsequent phases are designed and queued.

- **Phase A, mobile PWA capture-only.** Status: shipped v0.8.0. See [`../CHANGELOG.md`](../CHANGELOG.md).
- [`phase-b-server-live-streaming.md`](phase-b-server-live-streaming.md) (1410 lines). Status: designed. Server-live streaming as a paid-tier feature: WebSocket protocol, Parakeet 0.6B v3 streaming on midboy2, Qwen 3.6 35B rolling summary, tier gating, cost modeling, and a 5-phase implementation plan. All 4 open questions are locked.
- [`phase-b3-integration-plan.md`](phase-b3-integration-plan.md) (161 lines). Status: integration memo (2026-05-26). The merge/wiring target after the four parallel Phase B.3 chunks land — A (AudioWorklet), B (Opus codec utilities), C (NeMo streaming), D (Sortformer streaming diarization) — landed up front only as an env scaffold that defaults OFF so v1 production behavior is unchanged.
- [`phase-b3-nemo-streaming-spike.md`](phase-b3-nemo-streaming-spike.md) (268 lines). Status: spike report. NeMo cache-aware streaming does not produce usable output on the `parakeet-tdt-0.6b-v3` checkpoint (its full-context-trained encoder emits tokens for the first chunk then goes silent), so the spike instead ships a `/transcribe-stream-v2` draft+finalize endpoint as session-stateful pseudo-streaming on the existing checkpoint.
- [`phase-b3-sortformer-spike.md`](phase-b3-sortformer-spike.md) (385 lines). Status: ship-ready stage-3 follow-up (not yet wired). NVIDIA Sortformer 4-speaker streaming diarization (`diar_sortformer_4spk-v1`): ~244 MiB resident at fp16, ~200x realtime, co-resides with the three existing meet- services on midboy2 GPU 0, and passes a 10-way concurrent stress test; backend WS wiring is a separate 2-3 day change.
- [`runbook-streaming-v2.md`](runbook-streaming-v2.md) (278 lines). Status: live operations runbook. The operations view of the v1.0.0 → v2.1.0 live-streaming stack: container topology (`meet-parakeet-stream-svc`, `meet-sortformer-svc`, `meet-speaker-svc` on midboy2), the env flags that gate v2.x behavior, and how to deploy, roll back, smoke-test, and inspect logs.
- [`phase-c1-native-ios-design.md`](phase-c1-native-ios-design.md) (1386 lines). Status: designed. Native iOS plus Core ML plus watchOS extension. Swift and SwiftUI are required for Watch. FluidInference and FluidAudio Swift Package provide Parakeet 0.6B at 110x RTF on M4 Pro. Apple Foundation Models 3B, available on iOS 26+, provide the on-device LLM. The doc contains a 5-phase, 8-10 week plan for a v1 closed beta.
- **Phase C-2, watchOS extension on top of C-1.** Status: future.
- **Phase C-3, native Android plus NNAPI.** Status: future.

## Bulk operations

- [`bulk-audio-import-design.md`](bulk-audio-import-design.md) (1054 lines). Status: designed. `/import` page for the 526-file audio backlog. Covers the job queue, in-process asyncio for v1, Arq plus Redis migration in v4, per-file pipeline, speaker auto-linking from the `Call with X` filename pattern, and a 4-phase plan estimated at about 5 working days.

## Conference rooms

- [`conference-room-design.md`](conference-room-design.md) (1136 lines). Status: Phase 1 shipped, Phases 2-5 designed. Multi-room native model, USB mic primary path, chunk-based recording, room abstraction, satellite integration, and future administrative lifecycle.

## Speaker intelligence

- [`speaker-intelligence-design.md`](speaker-intelligence-design.md). Status: live (shipped v3.42.0–v3.45.0, on prod and dogfood). How Meeting-Ops turns an anonymous diarized transcript into stable, named people recognised across meetings, and how naming a speaker once fixes them everywhere. Covers the three tables (`speaker` / `speaker_voice_sample` / `speaker_session_link`) with embeddings stored as raw little-endian float32 bytes; pyannote diarization in `meet-speaker-svc` with the clustering threshold; cosine identification against enrolled centroids at `SPEAKER_IDENTIFY_THRESHOLD`; persistent UNNAMED auto-created profiles (v3.43); the anti-poisoning enrollment consistency floor + idempotency (v3.42); the key dynamic-name-rendering architecture (v3.44 → v3.45) where the display name is resolved live at serve time from the current profile via `hydrate_diarized_*`, so a rename is one row update and `apply_rename_to_history` only fixes summary free text off the request path; the rename / re-summarize flow; and a table of the `SPEAKER_*` env knobs. Real `file:function` references throughout.

## Agent platform (planning)

- [`agent-platform-roadmap.md`](agent-platform-roadmap.md). Status: planning. The next arc — conversational + app-driving + voice agent. Inventories the existing foundation (per-meeting + cross-meeting RAG in `ai_chat.py` + Qdrant, the Brigade FalkorDB graph, the 8 read-only MCP tools, the `agents.py`/`agent_tools.py` framework, TTS/STT building blocks), names the gaps, and lays out a 4-phase plan (solidify RAG + graph → safe app-control MCP write tools → agent runtime → voice) plus the key design decisions and operational context.
- [`agent-platform-phase-2-design.md`](agent-platform-phase-2-design.md) (104 lines). Status: design, awaiting ratification. The Phase 2 companion to the roadmap: a small, safe set of write/control MCP tools under a propose → user-confirms → mutate pattern, with short-lived scoped one-shot confirmation tokens, tier/org enforcement at the tool boundary, an audit trail, and bounded blast radius (no destructive bulk ops, and no `send_email` in v1).
- [`kg-and-agent-chat-plans-2026-06-06.md`](kg-and-agent-chat-plans-2026-06-06.md) (62 lines). Status: design-swarm plan (no code written). Decision-ready briefs for a person-centric `/knowledge-graph` page (reuse the 3D Brigade viewer, org-scoped, node-capped) and an Agent Chat upgrade (tier-gate provider config first, then an enterprise-gated, allow-list-validated, local-models-only per-chat model picker), with first-build steps for each.

## Ecosystem integration

- [`brigade-integration-design.md`](brigade-integration-design.md) (1332 lines). Status: designed. Meeting-Ops to Brigade graph writer and 3D viewer. Writes Meeting, Speaker, ActionItem, Topic, and Decision nodes to Brigade FalkorDB on session completion. Tenancy modes are `shared`, `per_org_graph`, and `per_org_instance` via environment variable. Includes the HIPAA path for Legacy1.
- [`mcp-hosted.md`](mcp-hosted.md) (212 lines). Status: live. The hosted MCP endpoint: the same FastMCP server that runs locally over stdio, exposed at `/mcp` over streamable HTTP so any AI client connects with just a URL and a Personal Access Token (`mops_pat_…`) and runs as that user's organization and RBAC scope. Covers PAT generation and client setup.

## Deployment and distribution

- [`appliance-extraction-design.md`](appliance-extraction-design.md) (530 lines). Status: designed. Extract appliance build to `UC-Meeting-Ops-Appliance` via git submodule for enterprise on-prem packaging.
- [`screen-recording-design.md`](screen-recording-design.md) (1268 lines). Status: Phase 1 designed. Personal screen plus audio MVP, capture paths, browser constraints, and implementation plan.
- [`image-registry-decision.md`](image-registry-decision.md) (143 lines). Status: decision recorded, implementation deferred. Recommends the Forgejo container registry at `git.unicorncommander.ai` as canonical image distribution.

## Accounts, tenancy, and data lifecycle

- [`consumer-signup.md`](consumer-signup.md) (69 lines). Status: shipped v3.2.0 but inert (activation-gated). Self-serve email/password signup for the free tier on the app's own HS256 JWT path (enterprise Keycloak SSO untouched): a private per-user personal org, email verification plus resend, and account-enumeration-safe endpoints — all held behind `ALLOW_REGISTRATION=false` until activated.
- [`data-retention.md`](data-retention.md) (23 lines). Status: live. Retention across Garage audio, Postgres, Qdrant, and the Brigade subgraph: off by default, with an `MEETING_RETENTION_ENABLED` / `MEETING_RETENTION_DAYS` deployment default, per-org and opt-in per-room (`Room.retention_enabled`, migration 049) overrides, a `legal_hold` exemption, and a daily Arq hard-delete task (`0` retains indefinitely).
- [`workspaces-two-level-design.md`](workspaces-two-level-design.md) (76 lines). Status: design / proposed (2026-06-08, not built). Add a lightweight second level (Spaces) inside the Org tenant: the Org stays the isolation, billing, and people boundary (shared speaker library, contacts, knowledge graph) while a Space only groups meetings — fixing the "my speakers disappeared in another workspace" problem.

## Production launch (`meeting-ops.unicorncommander.ai`)

Provisioning and go-live for the production VPS instance, plus the storefront content and the surrounding centerdeep-VPS housekeeping.

- [`deploy-unicorncommander-vps.md`](deploy-unicorncommander-vps.md) (264 lines). Status: manual runbook. Idempotent, step-by-step provisioning of the production centerdeep VPS (Hostinger KVM8) for `meeting-ops.unicorncommander.ai`; each step lists its precondition, command, and verification, and stops at any failure rather than patching around it.
- [`prod-launch-checklist.md`](prod-launch-checklist.md) (243 lines). Status: checklist (v3.20.0, invite-only). What is live versus intentionally held (Stripe and Postmark env unset, `ALLOW_REGISTRATION=false`), plus the reversible top-to-bottom checklist for opening the prod URL to real paying customers.
- [`centerdeep-archive-state.md`](centerdeep-archive-state.md) (122 lines). Status: operational record. The Center Deep brand archival (2026-05-29): exactly which containers to stop, in what order, and the per-container `docker start` revert command, so a future session can resurrect any piece without grepping git history.
- [`dataintel-auth-repoint.md`](dataintel-auth-repoint.md) (143 lines). Status: migration record. Re-point the surviving `dataintel` app off the standalone `centerdeep-keycloak` onto the shared `auth.unicorncommander.ai` (uchub realm) so `centerdeep-keycloak` can retire; captures current state, target state, and the migration sequence.
- [`launch-assets.md`](launch-assets.md) (35 lines). Status: storefront content. Meeting-Ops storefront launch assets for the UC / Ops-Center card: the launch-critical logo path, screenshots, the one-line value prop, and the three headline features.

## Audits and reviews

- [`audit-code-2026-06-24.md`](audit-code-2026-06-24.md) (109 lines). Status: point-in-time audit (worktree `fix/upload-status-ws-auth` @ `7568e29`). Code, architecture, and security audit: notes the improved controls, then the top remaining risks — forward-auth still failing open when its shared secret is absent, upload/import provenance writing the historical recording time into `created_at` (which watchdog and retention read as processing age), and the paid server-live WebSocket being broken for native-OIDC and ordinary non-superuser clients.
- [`audit-ux-2026-06-24.md`](audit-ux-2026-06-24.md) (378 lines). Status: point-in-time audit (`frontend/src/`, recommendations only). UI/UX and frontend quality audit: the highest-risk problems are trust failures that render empty or forever-processing states instead of explicit failure (a Sessions API error becoming "No Sessions Found", spinner-forever upload paths, mobile claiming "server is processing" after a failed finalize), plus two competing theme systems and expensive multi-megabyte first-load chunks.

## Roadmap (backlog)

The `roadmap/` subdirectory holds queued feature designs not yet scheduled.

- [`roadmap/post-process-split.md`](roadmap/post-process-split.md) (110 lines). Status: backlog / v3.24+. Don't auto-split during recording (the v3.23.1 silence-split bug fragmented one 31-minute recording into 6 sessions); make meeting-splitting a user-initiated post-processing decision computed with full-transcript context, keeping the audio file whole, reviewable, and reversible (`parent_session_id` / `is_split_parent`).

## Reading order for new developers

1. Read [`compute-economics.md`](compute-economics.md) first. It explains why the architecture is browser-first and why server compute is rationed.
2. Read [`phase-b-server-live-streaming.md`](phase-b-server-live-streaming.md) and [`phase-c1-native-ios-design.md`](phase-c1-native-ios-design.md) in either order. Together they explain the forward mobile and native roadmap.
3. Read [`bulk-audio-import-design.md`](bulk-audio-import-design.md). It is concrete near-term implementation work.
4. Skim the conference-room, Brigade, appliance, and screen-recording documents as needed for the feature area being changed.
5. Read [`speaker-intelligence-design.md`](speaker-intelligence-design.md) when working on diarization, speaker identification, voiceprints, or the speaker-naming UI. It documents the live persistent-identity and dynamic-name-rendering system.

This file is hand-maintained alongside design-document commits. It can lag behind the directory. If exact counts matter, verify them with `wc -l docs/*.md`.
