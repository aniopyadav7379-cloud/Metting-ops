## [Unreleased]

## [3.58.0] - 2026-07-25

### Added
- **Contact-Ops / Customer-Ops meeting federation** — meetings can be projected to
  federated cockpits behind a signed, cursor-paginated read API with per-summary
  approval gating.
- **Workspace action items view** — action items are surfaceable across a workspace
  rather than only per session.
- **Glitter Mane medical-visit integration surface** — an authenticated
  machine-to-machine endpoint (`/api/integrations/medical-visits`) that accepts a
  visit audio upload and reuses the Meeting-Ops STT + diarization pipeline. Gated by
  JWT audience and an `azp` actor allowlist.
- **Vector model provenance in Qdrant** — every indexed point now records
  `dense_embedding_model`, `sparse_embedding_model`, `index_schema_version`,
  `indexed_at`, and the measured `dense_embedding_dimension`. A collection can retain
  points across a model change when dimensions happen to match; recording the model
  beside each point makes that state observable and lets a reindex prove a workspace
  is homogeneous before search relies on it.
- **Knowledge-graph sync is observable and retryable** rather than fire-and-forget.

### Changed
- Semantic search now defaults to the Qwen3 embedding and reranker models.
  Deployments that pin `EMBEDDING_MODEL` / `RERANKER_MODEL` in their environment are
  unaffected until they are migrated deliberately.
- Project-Ops action-item integration now uses a workspace-bound Brigade token for
  proposal submission and read-only lifecycle refresh. Meeting-Ops checkbox changes no
  longer propagate Task status; Project-Ops remains the sole owner of its Task status.
- Speaker processing (diarization + embeddings) routes to the dedicated Midboy2 P40;
  STT remains on the separate Parakeet node. Opt-in so a generic `compose up` cannot
  silently steal the routing.
- The pre-record consent prompt has been removed.
- Unidentified-speaker review flow streamlined.
- Meeting workflows and white-label reports polished; stale meeting copy and
  transcript logs removed.

### Fixed
- Summaries now produce usable meeting briefs and are persisted reliably; settings
  logs no longer leak values.
- Invalid reprocess audio is rejected before it reaches GPU work.
- Summary approval controls align with session access rather than a separate,
  looser check.
- Meeting invitation secrets hardened (hashed storage + delivery tracking). v2
  issuance ships dark behind `MEETING_INVITE_V2_ISSUANCE_ENABLED`; existing v1
  redemption stays available.
- Federation approval and projection gaps closed.
- Project-Ops action lifecycle reconciled.

### Performance
- Frontend feature runtimes are deferred rather than loaded eagerly.

### Migrations
- `053_invitation_hashes_delivery`
- `054_project_ops_action_lifecycle`
- `055_federation_summary_approval`
- `056_brigade_sync_observability`

Chain is linear off `052_beta_invite_codes_emailed_at`; forward-only.

## [3.57.3] - 2026-07-17

### Changed
- Docs/README refresh: README badge + status brought to v3.57.x reality (chunked local pass,
  session self-heal, canonical landing, **native iOS app shipped on the App Store** — was
  still listed as in-flight), new `docs/local-only-mode.md` (privacy-mode pipeline + device
  gates), new `docs/manual-test-plan.md` (human QA walkthrough of the ten customer-visible
  surfaces), docs index updated.
- Removed the retired generic-mic `frontend/public/brand/meeting-ops.png` (unreferenced;
  the officer-unicorn `meeting-ops-mark.png` is the only mark) so it can never ship again.

## [3.57.2] - 2026-07-17

### Changed
- Scroll-reveal safety: sections render pre-shown under `?static=1` (capture/audit tooling —
  headless full-page shots have no scroll, so IntersectionObserver never fired and everything
  below the fold captured as a void) and under `@media print` (PDF-save of the page was blank
  below the fold). Real visitors keep the animation; reduced-motion was already handled.

## [3.57.1] - 2026-07-17

### Changed
- **Officer-unicorn watermark behind the hero headline** (Aaron's ask): the mark ghosted at
  ~10% opacity, desaturated, radial-masked so it fades before the subhead — brand presence
  without costing headline contrast.
- **Branded static pre-mount shell** replaces the bare "Loading… if this persists, the app
  failed to mount." (+ its stale tagline — the source of an external reviewer's outdated
  quote). The served HTML now carries the unicorn mark, the settled tagline, a one-paragraph
  pitch, Start-free + App-Store CTAs, and a navy inline background (the page is branded even
  before the CSS bundle arrives). Signed-in users deep-linking into the app don't get the
  marketing flash (pitch hides on evidence of a session); if the bundle genuinely fails, the
  hint upgrades to actionable copy after 8 s. Crawlable noscript summary retained.

## [3.57.0] - 2026-07-17

### Changed
- **Local-only full-quality pass now runs chunked.** New `inBrowserSTT.transcribeLong()` processes
  whole-meeting audio in ~5-minute windows, each cut at the quietest 100 ms frame near the
  boundary (an inter-sentence pause) so words aren't sliced in half, with a yielding mel loop
  (the old whole-meeting sync mel loop was the single biggest freeze source) and a macrotask gap
  between windows. The page stays responsive for the entire pass and each window stays inside
  Parakeet-TDT's ~24-minute long-form envelope — long-meeting transcripts get *more* accurate,
  not just cheaper. Live progress ("Transcribing locally — 34 of ~90 min (38%)") replaces the
  "works the machine hard" banner. With per-window compute bounded, decode memory is the binding
  constraint, so the `shouldRunFullLocalPass` cap on sub-8 GB devices doubles to ~1 h of audio
  (8 GB-class/unknown stays ~3 h; desktop-fallback/WASM still never runs the pass).
- **Knowledge-graph endpoint honors shared sessions.** `/api/sessions/{id}/brigade-graph` was the
  last session-detail endpoint still resolving strictly against the caller's active org; it now
  goes through `resolve_session_for_user` (min view) like the rest of the session-detail API, so
  the Knowledge Graph tab works on any session the other tabs can open. No-access sessions still
  404 (no existence leak).
- App tab/SERP title settled: "Meeting-Ops — Meetings become memory, decisions, and work"
  (replaces the dangling "Intelligence for your conversations (whether or not they are)"); the
  magicunicorn.dev preview page's titles aligned to the same line.
- (deploy, dogfood) oauth2-proxy `OAUTH2_PROXY_COOKIE_EXPIRE` 24h → 168h. The absolute cookie
  lifetime forced a daily re-auth redirect even with a live Keycloak session. Session authority
  now lives in the realm policy, set deliberately 2026-07-17 on BOTH uchub realms (bigboy +
  commander): 3 d idle / 30 d max (remember-me 30 d / 90 d), replacing 12 h / 48 h — the values
  behind the "every button silently dead" session deaths in the 2026-07-16 user test.

### Fixed
- (ops, both stacks) `STRIPE_API_KEY` in the deploy envs was an **expired** live key — every
  direct Stripe call from MO (checkout, portal, subscription lookups) failed. Replaced with the
  account's current key on centerdeep + bigboy (old envs backed up). While in there, verified the
  long-suspected annual mapping is actually correct: `STRIPE_PRO_ANNUAL_PRICE_ID` → live
  "$150/yr Pro Annual launch 150", monthly → "$15/mo Pro Monthly launch 15".

## [3.56.0] - 2026-07-16

### Fixed
- **Re-detect can now reduce the speaker count.** `_assign_speakers_from_diarization` took the
  relabel-in-place path over a prior pyannote-segmented result (segments with no text to
  preserve), which kept stale `raw_label`s, let no-overlap segments keep their old speaker, and
  never rebuilt the `speakers` roster — so re-detecting with fewer speakers (e.g. 4 → 2) was a
  no-op. Stored pyannote turns are now replaced outright with the fresh diarization, superseded
  raw labels are dropped on relabel, and the roster is rebuilt from final segment labels.
- **Rediarize background task no longer loses cross-org sessions.** The endpoint resolved a
  session via the cross-org access fallback but passed the caller's active org into the
  background task, whose strict org filter then found nothing ("session not found", status stuck
  at `queued` forever). The task now looks up by pk (endpoint already did authz) and receives the
  session's own org id.
- **Auto re-detect works again.** The frontend sent `clustering_threshold` with Auto mode, which
  the speaker-svc now rejects with HTTP 400 (env-var-only since its VRAM-churn fix) — failing the
  whole re-detect. Auto sends a plain `{}`; the Strict/Balanced/Loose selector is gone; the
  backend also defensively strips a stray threshold from stale cached frontends.
- **Duration formatters floor fractional seconds** — no more `93:7.69320499999958` on the
  Sessions list and recording surfaces (6 formatters).

### Changed
- **Session page polish.** All 12 card headers (AI Summary, Transcript, Participants, Action
  items, …) got explicit `text-gray-900` — they rendered white-on-white inside the light cards.
  Opening a session now scrolls the app shell to the top of the page (scoped per session, so
  going back keeps your place in the Sessions list).
- **Speaker panel parity + honest live-summary copy.** The empty speaker state gained the same
  Auto checkbox / speaker-count control as the populated header, and the live-summary panel
  explains what actually happens (idle vs listening states, slice threshold, and that the full
  summary/transcript/speakers come when you stop) instead of the confusing recovery-banner text.
- Version bump to `3.56.0` across the app shell and release metadata.

## [3.55.0] - 2026-07-08
- **Ops-Center entitlement sync at SSO login.** Meeting-Ops now opportunistically checks the central Ops-Center entitlement service during Unicorn Commander SSO callback and upgrades the local user/org comp to Pro only when OC returns `meeting_ops_access` plus a paid tier. The sync is dormant unless `MEETING_OPS_ENTITLEMENT_URL` is set, uses `MEETING_OPS_ENTITLEMENT_HTTP_TIMEOUT` (default `3.0`) and `MEETING_OPS_ENTITLEMENT_COMP_DAYS` (default `32`), and fails open on any HTTP, parse, or entitlement error so login is never blocked.
- **Internal routing note.** Point `MEETING_OPS_ENTITLEMENT_URL` directly at the internal Ops-Center `/api/v1/entitlements` endpoint over Tailscale. Do not route this through Cloudflare; leaving the env unset keeps the feature dormant and byte-identical to prior releases.
- Version bump to `3.55.0` across the app shell and release metadata.

## [3.54.0] - 2026-07-08
- Launch Console invite-code emailing: new superuser-only `/api/admin/invite-codes/send` and `/api/admin/invite-codes/send-cohort` routes render cohort-specific invite emails, support dry-run preview, skip already-emailed codes via new `beta_invite_codes.emailed_at`, and audit-log successful sends.
- Launch Console UI: `frontend/src/pages/admin/CompsAdmin.tsx` now has per-row `Email…` and batch `Email codes…` actions with preview-first CSV/paste flow and explicit send confirmation.
- Version bump to `3.54.0` across the app shell and release metadata.

## [3.53.0] - 2026-07-08

### Added
- **Launch Console — in-app comps + invite-code oversight (superuser).** A new
  `/admin/comps` page ("Launch" in the sidebar, platform-superuser only) to see
  and organize the launch cohort without shelling into the container: summary
  cards (active / expired / permanent comps, codes available / redeemed), a
  table of every non-free user with tier, status, expiry and one-click Revoke,
  and a table of all invite codes with cohort/status filters, copy-available,
  and CSV export. Grant or extend a comp by email and mint invite codes inline.
- **Comp + invite-code admin API (superuser-gated).** New
  `GET /api/admin/comps` (+ `/api/admin/comps/summary`),
  `POST /api/admin/comps/grant`, `POST /api/admin/comps/revoke`, and
  `GET /api/admin/invite-codes` (list, next to the existing mint). Grant reuses
  the same `comp_personal_org_to_pro` helper the invite-redemption path uses
  (sets user tier + `tier_expires_at` AND the personal org plan); revoke mirrors
  `grant_pro --revoke` + `revert_expired_comps` exactly. Every mutation is
  audit-logged. All routes gate on `is_superuser` (not `require_admin`, which
  every self-serve user satisfies for their own personal org).

## [3.52.0] - 2026-07-07

### Fixed
- **Comps now unlock Pro compute (they gated on only one of two surfaces).**
  `require_feature` checks BOTH the user's tier AND the active org's plan, but
  `grant_pro` set only `user.tier` and the invite-code path set only `org.plan`
  — so a comped user still 403'd on server-compute Pro features. Both comp paths
  now set `user.tier` + the personal `org.plan` together (mirroring a real Stripe
  subscription), time-limited via `tier_expires_at`, and `revert_expired_comps`
  reverts both surfaces on expiry.

### Added
- **Invite-code comps are now time-limited Pro.** `comp_personal_org_to_pro`
  sets tier + expiry (default 30 days) + org plan; an OPTIONAL invite code at
  signup (when `REQUIRE_INVITE_CODE` is off) grants a time-limited Pro comp while
  a no-code signup stays free (a non-empty but invalid code 403s).
- **`scripts/gen_invite_codes.py`** — mint N single-use beta invite codes for a
  cohort: `python -m scripts.gen_invite_codes --count 100 --days 30 --cohort meeting_ops_v1`.

## [3.51.0] - 2026-07-07

### Added
- **Time-limited Pro comps (no Stripe, no card).** An admin can grant an invited
  user Pro for a bounded period that auto-reverts to free — for a "free month"
  cohort without payment details or a cancel-trap. New `users.tier_expires_at`
  (migration 051, additive nullable timestamptz; NULL = permanent). New
  `scripts/grant_pro.py` CLI: `python -m scripts.grant_pro <email> [--days 30]
  [--tier pro] [--founding] [--cohort meeting_ops_v1] [--revoke]`. A new
  `session_watchdog.revert_expired_comps()` pass (wired into the 30-min cron)
  reverts `tier→free` + clears the column once `tier_expires_at < now()`
  (bounded, idempotent, superusers exempt). The Stripe webhook clears
  `tier_expires_at` on a paying subscription — so a comped user who later
  subscribes (even at the same tier) is never auto-reverted.

## [3.50.0] - 2026-07-07

### Added
- **In-app "Upgrade to Pro" checkout.** The Stripe checkout backend + billingApi
  client existed but nothing in the UI called them. Wired the full path: a shared
  `utils/checkout.ts` (`beginProCheckout` / `manageSubscription`), a single
  Pro-price source of truth (`constants/pricing.ts`), a Settings → "Plan & billing"
  panel (`BillingSettings.tsx`) with upgrade + Stripe-portal "Manage subscription",
  and real in-app CTAs on `Pricing.tsx`, `Landing.tsx`, and `UpgradeBanner.tsx`
  (anon → `/signup?intent=pro`, signed-in → checkout). Terms + refund policy linked
  at every point of sale. Frontend-only; no backend/Stripe/env change.
- **Launch-pricing anchor on the Pro cards.** Struck-through $20 next to $15 with a
  "Launch pricing — 25% off" note on both Landing + Pricing Pro cards
  (`PRO_LIST_PRICE_USD` + `PRO_LAUNCH_DISCOUNT_PCT`, display-only; the charged
  amount stays the server-side Stripe price).

### Changed
- **Upload completion pass runs STT and diarization concurrently.**
  `run_upload_pipeline` (`backend/api/uploads.py`) now launches `diarize()` as an
  asyncio task before awaiting Parakeet STT and awaits it in the diarization stage
  (they read the same audio independently on separate GPUs, merged by timestamp) —
  mirroring `_run_session_reprocess`. Falls back to a synchronous call on launch
  failure; STT failure cancels the task. Saves ~the diarization duration per
  uploaded meeting.

### Fixed
- **Summaries silently failed with a 401 after the summarizer was repointed at the
  LiteLLM gateway.** `_direct_summarizer_provider()` only read
  `MEETING_OPS_LLM_API_KEY` / `MEETING_OPS_SUMMARIZER_API_KEY` (unset in deploy);
  the authenticated gateway (`unicorn-litellm:4000`) then returned 401 "No api key
  passed in", `finalize_session_job` burned its 5 retries, and the summary was
  permanently dropped best-effort — so new meetings got no summary and renaming a
  speaker never refreshed it. Now falls back to the suite-standard
  `OPENAI_API_KEY` / `LITELLM_API_KEY` (MO-specific vars still take precedence).
  No env/compose change.
- **Sessions could wedge in "processing" forever after a transient summarizer
  blip.** A summary-stage failure left `status=processing` + `needs_summary` with a
  dead `processing_job_id`, and the worker drift-guard turned any re-enqueue into a
  no-op. New `session_watchdog.redrive_stuck_summary_sessions()` (wired into the
  30-min cron) clears the drift-guard and re-enqueues finalize on the interactive
  lane, bounded by `SESSION_WATCHDOG_SUMMARY_REDRIVE_MAX` (5) /
  `..._COOLDOWN_MINUTES` (10) so a genuinely-broken LLM stops instead of looping.
  Exhausted rows are later promoted to `completed` (transcript visible).
- **Fresh consumer signups saw the full appliance "Admin" nav.** Self-serve users
  are org-admins of their own personal org, which exposed Rooms / Speakers /
  Agents / Bulk Import as if they were appliance admins. Gate the appliance/fleet
  surfaces (Rooms, Agents, Bulk Import) on the active org's enterprise plan or
  superuser; consumer org-admins keep Speakers under a "Workspace" heading. Also
  pipe `VITE_ROOM_MODE_ENABLED` through the Dockerfile/VPS compose (VITE_* vars
  only reach the bundle as build args).

## [3.49.0] - 2026-07-02

This release **reconciles the integration that had been running in production**
(meeting-ops.unicorncommander.ai) back onto `main`. Every change below was live
and verified in prod before this commit; `main` had drifted behind the deployment.
It integrates the previously-separate branches `feat/landing-product-page`,
`feat/upload-meeting-date-from-file`, and `feat/concurrent-diarize-and-24k-summary-tuning`,
plus auth-hardening work that existed on no branch.

### Security
- **Forward-auth trust is now fail-CLOSED.** `auth/proxy_trust.py` previously
  **trusted inbound `X-Auth-Request-*` / `X-Forwarded-User` / `Remote-User`
  headers when `PROXY_AUTH_SHARED_SECRET` was unset** — which is exactly the
  native-OIDC production case, so a forged header could impersonate a user. Unset
  secret now means those headers are ignored, not trusted. Paired with a Traefik
  **`meet-strip-auth` edge middleware** that strips those headers off inbound
  requests (defence in depth), and **Brigade JWT verification**
  (`BRIGADE_JWKS_URL` / `TRUSTED_ISSUER` / `EXPECTED_AUDIENCE`).

### Added
- **Upload date from the file, not ingest time** (migration `050_upload_client_modified_at`,
  additive nullable `upload_jobs.client_modified_at`). The browser's
  `File.lastModified` is carried through `/api/uploads/start` into meeting
  provenance and ranked (`CONF_CLIENT_MTIME=0.55`, above server mtime), so an
  uploaded meeting dates to when it was recorded, not when it was ingested.
- **Summary map-reduce tunables** (`MEETING_OPS_SUMMARY_MAPREDUCE`,
  `MEETING_OPS_SUMMARY_CHUNK_TOKENS`). A 14k chunk keeps both the map path and the
  single-call path inside a 24k-context summarizer slot, and map-reduce gives long
  meetings full coverage instead of single-call truncation at the transcript cap.
- **Native-OIDC WebSocket auth** — `ws_auth.py` adds an `mo_uc_session` cookie
  fallback + `resolve_session_org` so the per-workspace billing gate works over WS
  on native-OIDC deploys.
- **Landing / Pricing marketing redesign** (`Landing.tsx`, `Pricing.tsx`, brand
  asset, SEO/OpenGraph/Twitter meta in `index.html`).

### Changed
- **Diarization now runs concurrently with transcription.**
  `_run_session_reprocess` launched Parakeet STT and pyannote diarization
  serially even though they read the same audio independently on separate GPUs and
  merge afterward. Diarization is now an `asyncio` task started before the STT
  await and awaited after, cutting that stage from `STT + diarize` to
  ~`max(STT, diarize)`. Concurrency-safe (provider DB setup precedes the parallel
  section; `diarize()` is HTTP-only; the task is cancelled if STT raises; degrades
  cleanly to transcript-only). Verified on a 76-min meeting — diarize started ~5s
  in, overlapping ~43s of STT. See
  `docs/inference-pipeline-2026-07-02-concurrency-and-24k-tuning.md`.

## [3.48.2] - 2026-06-21

### Added
- **Chunked long-audio diarization (speaker-svc), on one GPU.** Meetings over
  `CHUNK_THRESHOLD_SECONDS` (8h) are split into `CHUNK_WINDOW_SECONDS` (60-min)
  windows, each diarized single-pass on the GPU, then stitched into one global
  speaker set by wespeaker-embedding cosine (`CHUNK_STITCH_THRESHOLD`). Mirrors how
  parakeet-svc chunks long audio for STT. Bounds pyannote's O(n^2) clustering for
  all-day recordings without a second card. At/under the threshold stays
  single-pass — pyannote's global clustering is most accurate, and the 4070 handles
  ~8h single-pass at ~6 GB in ~5 min (validated). `MAX_AUDIO_SECONDS` is now a 24h
  absolute ceiling instead of the long-meeting gate. Same `/diarize` response shape;
  downstream `identify_speakers` unchanged.

### Fixed
- **"Can't log out" — the Keycloak SSO session survived logout.** Two causes:
  (1) `meet-oauth2-proxy` was missing `OAUTH2_PROXY_WHITELIST_DOMAINS`, so
  `/oauth2/sign_out` refused to forward to Keycloak's end-session (deploy-config:
  added `.magicunicorn.dev`). (2) Even forwarded, KC kept the session alive
  because the request had no `id_token_hint`, so the user was silently
  re-authenticated. New backend `GET /api/auth/sso-logout` reads the KC ID token
  (injected by oauth2-proxy via `SET_AUTHORIZATION_HEADER`), passes it as
  `id_token_hint` so KC actually terminates the session, and derives the issuer +
  post-logout redirect from the token + request — **environment-agnostic** (no
  hardcoded host; works on every deploy domain). The frontend logout now redirects
  there instead of a hardcoded Keycloak URL (`AuthContext.tsx`).
- **Diarization silently capped meetings at the stale `MAX_AUDIO_SECONDS`.** The
  running speaker-svc was at 4h while the project intent was 8h, so a 5.8h upload
  was rejected (`audio too long`) and finished transcribed-but-not-diarized (0
  speakers). Raised to the 8h single-pass ceiling + 24h hard ceiling; bumped the
  backend `SPEAKER_SVC_TIMEOUT` 300s->1800s so long single-pass diarizes complete
  in the normal completion pass instead of timing out at 5 min.

## [3.48.1] - 2026-06-21

### Fixed
- **Speaker-tagging dropdowns disappeared from the session view.** Two causes:
  - *Frontend:* the session-detail page derived its `speakers`/`segments` from
    `transcription.*`, but an **empty array is truthy in JS**, so the
    `|| transcript_diarized` fallback never ran — the page read "0 speakers" and
    hid the inline speaker-tagging card on every tab except "Speakers". Now prefers
    the live-hydrated `transcript_diarized.speakers` and skips empty arrays, so the
    inline dropdowns + correct count come back. (`SessionDetails.tsx`)
  - *Backend:* the speaker endpoints (`list_speaker_links` + assign / rename /
    identify / rediarize / enroll / merge) were strictly org-scoped, so opening a
    session from another org you have access to (e.g. a personal-org recording while
    active in a different org) loaded the page but 404'd the tagger → "No diarized
    speakers detected." `_get_session_or_404` now resolves cross-org via
    `has_session_access`, mirroring `get_session` (same-org behavior unchanged).

## [3.48.0] - 2026-06-21

### Fixed
- **Paid tiers silently inherited Free upload limits.** `services/quotas.py`
  `TIER_DEFAULTS` only defined free/pro/enterprise, so `sync`/`basic`/`suite` orgs
  fell through to Free's 10h / 500 MB / 1-concurrent caps — a $35 Suite org was
  *more* restricted than a $20 Pro. Added explicit `sync`/`basic`/`suite` entries
  (sync/basic = free-equivalent, no server audio by design; suite = 5 GB / 8 / 250 h,
  ≥ Pro). Regression test: `tests/test_quotas.py`. (Suite numbers are sensible
  defaults — adjust to taste.)
- **The "Reprocess" button was a silent no-op for always-on / browser sessions.**
  The endpoint (`api/uploads.py`) required the reassembled wav on *local* disk and
  returned 410 when it was missing (evicted to Garage) or stale (the pre-v3.47
  truncated 30 s copy) — and the frontend never surfaced the error. Now: when audio
  chunks are on disk, reprocess **reassembles them** (byte-concat — the canonical
  full audio) so the manual button works for always-on/browser recordings and
  re-runs the full pass over the WHOLE meeting, not a stale wav; and the frontend
  (`SessionDetails.tsx`) **toasts** success/failure so a 403 (tier) or 410 (no
  audio) is no longer invisible.

## [3.47.0] - 2026-06-21

### Fixed
- **Critical: meetings were transcribed from only the first ~30 seconds of audio.**
  Browser / always-on recordings upload the meeting as ~30s MediaRecorder timeslice
  chunks; `_reassemble_full_audio` stitched them with ffmpeg's concat *demuxer*,
  which treats each chunk as a standalone container. But timeslice chunks are
  fragments (only chunk 0 carries a container header), so the demuxer decoded **only
  the first chunk** — a 24-minute meeting reassembled to a 30-second WAV, and the full
  pipeline (Parakeet 1.1B STT + pyannote diarization + speaker fingerprinting +
  summary + title) ran on just those 30 seconds. Now byte-concatenates the fragments
  back into the original continuous stream before decoding (verified on a real
  48-chunk / 24-min recording: reassembles to ~1424s vs 30s). Restores true
  browser↔upload parity. Existing affected sessions can be recovered by re-running
  reprocess — the per-chunk audio is retained on disk.

### Changed
- **Sidebar brand line simplified** to "Intelligence for your conversations" — the
  parenthetical wrapped to ~4 lines in the narrow rail; the wit ("…whether or not
  they are") stays on the loading splash + browser title.

### Documentation
- **README body positioning pass.** Reconciled the body with the SaaS-primary +
  open-source + enterprise reality (the v3.46.1 hero/footer were already done). TL;DR
  + comparison table now lead with the honest, durable differentiators — *no
  third-party AI, never trained on your data, runs anywhere (our cloud / your private
  cloud / air-gapped on-prem), open source* — and dropped the "$0 marginal cost / on
  GPUs you own" framing that implied self-host-only. "The moat: zero marginal compute"
  retitled **"Browser-first by design"** and reframed around user benefit; "Privacy &
  sovereignty" → **"Privacy, ownership & control"** (honest in SaaS: *we* run the
  models — not a third party — with self-host/on-prem as the full-sovereignty option).

## [3.46.2] - 2026-06-20

### Fixed
- **In-app sidebar tagline.** The left-sidebar brand line
  (`AppRouterSimplified.tsx`) still read "Meeting intelligence, self-hosted" — the
  one spot the v3.46.1 tagline change missed (it updated the browser title, loading
  splash, and Landing hero, but not the sidebar `<div>`). Now shows the brand line:
  *Intelligence for your conversations (whether or not they are)*.
- **Tighter punchline.** Shortened the parenthetical from "(whether or not they're
  intelligent)" to **"(whether or not they are)"** everywhere (sidebar, browser
  title, loading splash) — lets the reader complete the joke off "Intelligence for
  your conversations." Rotating taglines deferred to the future roadmap.

## [3.46.1] - 2026-06-20

### Changed
- **Brand voice + positioning (research-backed).** In-app web GUI tagline (browser
  title + loading splash) → **"Intelligence for your conversations (whether or not
  they're intelligent)"** — playful, on-brand for Magic Unicorn; replaces "Meeting
  intelligence you self-host." Public Landing hero + README repositioned to the
  enterprise / open-source standard: **"The open standard for conversation
  intelligence"** + an honest SaaS subhead (*understands every meeting — without
  harvesting your data or sending it to someone else's AI; host it with us, or run
  it yourself*). Dropped the "100% self-hosted / sovereign by design" hero overclaim
  since the product is SaaS-primary + open-source (sovereignty is now framed as the
  self-host/on-prem *option*, not the default). Grounded in audience + tagline-landscape
  research (enterprise-primary, individual on-ramp; "AI"/"magic" as headline claims
  test poorly with this audience; "open" is the one claim no competitor can make).

## [3.46.0] - 2026-06-20

### Added
- **In-app Help — Speaker Intelligence sections.** New end-user help topics on the
  Help page (`frontend/src/pages/Help.tsx`, "For everyone" tier): automatic speaker
  identification + persistent cross-meeting identity (stable handles like
  "Speaker 3F2A"), "name once → fixed in every past meeting, instantly," why
  confirming a speaker is safe (the anti-poisoning consistency floor), browser
  recording = full quality, and a strengthened Privacy-mode explanation.

### Documentation
- **README rewrite (accurate + current).** Reworked the top-level README around the
  browser-first moat, the speaker-intelligence system, and the *real* live stack —
  fixing long-stale references (it still claimed "Granite 3.3" for the LLM and
  "whisper.cpp" for STT; the actual stack is Qwen 3.6 35B-A3B-Vision via LiteLLM +
  in-browser/server Parakeet + pyannote `community-1`). Refreshed tiers, quick
  start, API summary, and status to v3.45.0.
- **New `docs/speaker-intelligence-design.md`** (engineer-facing): data model
  (speaker / speaker_voice_sample / speaker_session_link), diarization, cosine
  identification + thresholds, persistent auto-profiles (v3.43), the anti-poisoning
  enrollment floor (v3.42), and the dynamic name-rendering single-source-of-truth
  (v3.44–v3.45). Indexed in `docs/README.md` (last-updated → 2026-06-20).

## [3.45.0] - 2026-06-20

### Changed
- **Fully dynamic transcript speaker names — single source of truth.** Speaker
  names now render LIVE from the current `SpeakerProfile` at *every* transcript
  display surface — session detail (`get_session`), all exports (PDF/DOCX/TXT/SRT/
  JSON/markdown), AI-chat/RAG LLM context, and the always-on payload — via the
  session links (`hydrate_diarized_for_session`, which resolves the DB from the ORM
  object so no signature threading is needed). As a result `apply_rename_to_history`
  **no longer rewrites stored transcripts at all** — it only fixes the AI summary's
  free text (which can't be rendered live). A rename is now one profile update +
  a tiny summary touch, regardless of how many meetings the speaker appears in;
  the speaker's name lives in exactly one place. Ingest paths (satellite/websocket)
  and the Qdrant search index (manual reindex) are unchanged by design.

## [3.44.0] - 2026-06-20

### Changed
- **Live speaker-name rendering + instant renames (scale refinement).** The
  session-detail transcript now resolves each speaker's NAME live from the current
  SpeakerProfile via the session links (`hydrate_diarized_speaker_names`) at serve
  time, rather than relying solely on the name baked into each segment — so a
  rename shows in the transcript view immediately. The rename's history
  propagation (re-labeling past meetings' stored transcripts + summaries) now runs
  **off the request path** as a background task, so renaming a speaker that appears
  in many meetings returns instantly instead of blocking on N transcript rewrites.
  Read-only hydration (no ORM mutation), backward-compatible (segments without a
  resolvable link keep their stored name). Fully dynamic rendering across exports /
  RAG-index / satellite is mapped as a follow-on; those catch up via the
  background pass (or manual reindex).

## [3.43.0] - 2026-06-20

Persistent speaker identity — give every voice a stable identity and let naming
fix the past. (Backend; the naming-prompt UI follows.)

### Added
- **Auto-create stable profiles for unmatched voices.** A diarized voice that
  matches no enrolled speaker now auto-creates a stable, UNNAMED `SpeakerProfile`
  with a handle like `Speaker 3F2A` and enrolls its voiceprint — instead of a
  throwaway per-session "Speaker 1/2". The same voice matches the same profile in
  later meetings, so it keeps one consistent handle until a human names it. Covers
  both the first-meeting bootstrap (no enrolled speakers) and the no-match case.
  Guarded by a minimum clean-speech duration (`SPEAKER_AUTOCREATE_MIN_SECONDS`,
  default 4s) + the consistency floor; never overrides a confirmed label; toggle
  with `SPEAKER_AUTOCREATE_ENABLED`. `SpeakerResponse.auto_generated` flags these
  for the UI to surface for one-click naming.
- **Naming propagates to history.** Renaming a profile (e.g. the auto-handle → a
  real name) now re-labels every PAST meeting that speaker appears in — both the
  diarized transcript and the summary/insights text (cheap string ops, no re-STT)
  via `apply_rename_to_history`, and clears the auto-generated flag. New
  `POST /api/speakers/{id}/resummarize-history` regenerates the AI summaries of
  those meetings (the optional "re-summarize too" button; lightweight finalize,
  no re-transcribe).

## [3.42.0] - 2026-06-20

Speaker-identification quality: stop fingerprint poisoning + bring browser
recordings to upload-grade speaker ID.

### Added
- **Browser↔upload pipeline parity.** When an always-on (browser) recording is
  stopped and its audio is available server-side, `/finalize` now routes through
  the SAME full completion pass as an uploaded file — canonical Parakeet 1.1B STT
  + pyannote diarization + speaker **identification** — instead of the light
  summary-only finalize that left browser recordings with generic "Speaker 1/2".
  Privacy-mode / text-only / diarization-off sessions keep the light path so
  nothing extra leaves the device. Idempotent with the explicit `/finalize-audio`.

### Fixed
- **Speaker fingerprint poisoning.** `add_voice_sample` now enforces a consistency
  floor (`SPEAKER_ENROLL_CONSISTENCY_FLOOR`, default = the identify threshold):
  once a profile has an enrolled centroid, a sample whose cosine to it is below
  the floor — i.e. a mislabeled or mixed diarization cluster (two voices pooled) —
  is **no longer averaged into the voiceprint**. That averaging was drifting
  centroids toward the wrong voice and compounding mis-IDs across sessions
  (observed live: a 0.487-similarity "Gina" sample, a 0.514 "Vinny"). Enrollment
  is also **idempotent per source session** (re-confirming a speaker no longer
  double-weights that cluster). The first sample still bootstraps the identity.
  Ports the guard the account-level "My Voice" path already had to org profiles.

## [3.41.2] - 2026-06-20

### Fixed
- **`tts` readiness probe authenticates against the gateway** (companion to the
  v3.40.2/3.40.3 llm + infinity probe fixes). Once `KOKORO_ENDPOINT` points at the
  gateway, the old unauthenticated `/health` probe 401'd → prod read `tts: down`
  even though synthesis worked. It now tries an authenticated `/v1/models` first,
  `/health` fallback for a direct Kokoro (behaviour-neutral on bigboy). With this,
  every gateway-routed dependency (llm, embeddings, stt-via-direct-fallback, tts)
  reports honestly and prod `/health/ready` is fully green again.

## [3.41.1] - 2026-06-20

### Fixed
- **Gateway STT/TTS authenticate.** `get_api_key()` only returned the default
  (gateway) key for `service_kind == "llm"`, so the new gateway-routed STT + TTS
  providers got an empty key and the gateway 401'd ("No api key passed in"). It now
  returns the default key for every gateway-routed kind (`llm/stt/tts/embeddings/
  reranking`); native/in-cluster endpoints ignore the Authorization header, so it's
  harmless on the direct path. (Caught by the in-situ end-to-end check before any
  user traffic hit the flipped path.)

## [3.41.0] - 2026-06-20

### Added
- **Gateway audio: STT/TTS can route through the OpenAI-compatible gateway.** A new
  `OpenAITranscriptionProvider` (drop-in for `LocalParakeetProvider` — same signature
  + normalized return shape) lets MO call an OpenAI `/v1/audio/transcriptions`
  endpoint, i.e. the commander LiteLLM gateway (`model=parakeet` → the midboy1
  parakeet-openai-shim). `get_stt` resolves it for `provider_name` /
  `STT_DEFAULT_PROVIDER` in `{openai, gateway, litellm}` (endpoint from
  `STT_OPENAI_URL`/`OPENAI_API_BASE`, model `STT_OPENAI_MODEL` default `parakeet`,
  key via the org's STT key → gateway key). TTS already speaks OpenAI
  `/v1/audio/speech`, so Kokoro rides the same gateway by pointing `KOKORO_ENDPOINT`
  at it. This puts audio on the suite's **single, metered, cloud-failover-ready
  gateway rail** alongside LLM + embeddings.
  - **Native Parakeet stays the default/fallback** (`STT_DEFAULT_PROVIDER=parakeet`),
    so the critical transcription path is opt-in onto the gateway and instantly
    revertible. Verified end-to-end against the live gateway (real transcription +
    synthesis) before enabling; `STT_DEFAULT_PROVIDER` is now env-overridable in the
    unicorncommander compose (backend + worker anchor).

## [3.40.3] - 2026-06-19

### Fixed
- **`infinity` (embeddings) readiness probe authenticates against the gateway.**
  Same class as the v3.40.2 llm-probe fix: the probe did an unauthenticated
  `GET {base}/health`, which the LiteLLM gateway 401s (and hangs when authed),
  so prod read `infinity: down` even though embeddings/rerank work (verified
  `/v1/embeddings` → 200 with the key). It now tries an authenticated
  `GET {endpoint}/models` first (`INFINITY_API_KEY` / `LITELLM_API_KEY` /
  `OPENAI_API_KEY`), falling back to the unauthenticated `/health` a direct
  Infinity server exposes (behaviour-neutral on bigboy). Prod `/health/ready` is
  now fully green across all eight dependencies.

## [3.40.2] - 2026-06-19

### Fixed
- **`default_llm()` honors the configured gateway/LLM base.** The org-less default
  provider (used by the `/health/ready` `llm` probe + any background job without an
  org context) hardcoded `http://unicorn-litellm:4000/v1` — a container name that
  resolves on bigboy but **fails name resolution on prod** (centerdeep, no such
  container), so prod read `llm: down` even though summaries worked via the direct
  path. `DEFAULT_LITELLM_ENDPOINT` now reads `LITELLM_API_BASE` / `OPENAI_API_BASE`
  / `OPENAI_BASE_URL` first, falling back to the container name (behaviour-neutral
  on bigboy). The gateway key already reaches the container via the compose's
  `LITELLM_API_KEY → OPENAI_API_KEY` mapping, so the probe now authenticates and
  greens.

## [3.40.1] - 2026-06-19

Follow-ups to the v3.40.0 hardening + the prod (unicorncommander.ai) rollout.

### Added / Changed
- **Org-aware tier UI.** `/api/auth/me` now returns `active_org_features` (+ the
  workspace `plan`), and `useTierFeatures` returns the EFFECTIVE capability =
  AND(user tier, active-org plan) with a `limitedBy: 'tier' | 'workspace'` signal
  and superuser bypass — so the UI matches the per-workspace backend gate instead
  of showing enabled controls that then 403. The upgrade banner now shows a
  workspace-upgrade prompt when a paid user is acting in a Free workspace.

### Fixed
- **Optional `netifaces` import is guarded** — its absence can no longer drop the
  whole system-service module (and with it the auth-bearing `websocket_auto_summary`
  router).
- **Container healthcheck is liveness, not readiness** — switched to the shallow
  `/health` so a serving-but-degraded backend (an optional AI dep unreachable) is
  not marked unhealthy; `/health/ready` stays the deep diagnostic.
- **Prod deploy fixes (centerdeep):** carry `SECRET_KEY` on the worker anchor (the
  security-2 guard restart-looped the workers without it); attach the backend to
  `unicorn-network` so it can reach the shared `unicorn-redis` / `unicorn-qdrant`
  (they were on a network the compose didn't join); point the AI endpoints at the
  current mesh hosts (STT/diarization moved to midboy1, LiteLLM gateway to
  `litellm:4000`) — the remaining STT/embeddings/TTS reachability is a
  tailnet-ACL item.
- Aligned stale test fixtures that seeded free-tier users against paid-tier-gated
  endpoints.

## [3.40.0] - 2026-06-19

Enterprise/SaaS production-readiness hardening — a multi-agent audit (59 verified
findings) driven to completion: a critical SSO trust-boundary fix, per-workspace
billing correctness, and table-stakes security / reliability / observability /
data-lifecycle work. Browser-first moat unchanged; no API contract changes.
Deployed + live-verified on dogfood.

### Security
- **SSO forward-auth trust boundary (critical).** Identity headers (`X-Auth-Request-*`/`-Groups`, the latter drives `is_superuser`) are now honored ONLY when the request carries a matching `X-Proxy-Auth` secret (Traefik-injected → oauth2-proxy-passed). Closes an auth-bypass / superuser-escalation forgeable by any container on the shared docker network. Fails open until `PROXY_AUTH_SHARED_SECRET` is set, then enforces.
- **Fail-closed `SECRET_KEY` boot guard.** The backend refuses to boot on an unset / placeholder / <32-char signing key in a real deployment (removes the public hardcoded fallback that allowed JWT forgery). Dev/test/CI keep a labelled insecure fallback.
- **HTTP security headers** middleware (X-Frame-Options DENY / CSP frame-ancestors / nosniff / Referrer-Policy / HSTS on https).
- **WebSocket auth.** The job-progress, live-transcription, and remote-audio sockets now authenticate the handshake and scope by owner/org. `WS_REQUIRE_AUTH` (default true) is a kill-switch for instant rollback — now actually wired into the backend + worker compose env.

### Billing & multi-tenancy
- **Per-workspace server-compute billing (billing-1).** Paid server-compute now requires the **active organization's** plan to cover the feature, not just the buyer's global `User.tier`. A single Pro seat no longer unlocks paid compute in every org the buyer belongs to. Internal-service callers and superusers bypass; the check is threaded through every server-compute path (record/finalize/reprocess, summary/TTS, uploads, bulk import, speaker library, AI chat / cross-meeting search, agent-write). A paid user acting in an unpaid workspace now gets a clear `This workspace's plan does not include this capability` 403.
- **No double-billing (billing-2).** Starting a second Checkout while already subscribed returns the Billing Portal instead of creating a duplicate subscription.
- **Tenancy isolation.** Brigade knowledge-graph writes are fail-closed per org; async job status is org-scoped.

### Reliability
- Interactive upload pipeline recovery + watchdog (no longer strands on deploy/crash); deep `/health/ready` + `/api/ready` readiness probes (detect db/redis/qdrant/llm/stt outages); retryable failed summaries; connection-pool pre-ping; Arq transient-failure retries.

### Observability
- Privacy-safe Sentry init (PII scrub), React render error boundaries, HTTP metrics + request tracing.

### Performance
- Cursor pagination of session lists (tenants past ~2000 meetings can see their tail; the list no longer re-fetches every row's full transcript); a dedicated interactive Arq worker lane so bulk import no longer starves live finalize; trimmed analytics + GPU guards.

### Data lifecycle
- **Right-to-erasure on delete.** All session-delete paths now purge Qdrant embeddings + per-meeting chat history (previously orphaned).
- **Per-room retention opt-in.** New `Room.retention_enabled` (migration 049, default off): a room drives deletion only when explicitly opted in, otherwise sessions fall back to the org/deployment policy — closing the implicit 90-day-purge footgun from `default_retention_days`'s server default. The daily compliance purge stays DISABLED by default and honors legal holds.

### Notes
- The frontend tier UI (`useTierFeatures`) still reflects the user's global tier; the backend is now the per-workspace authority, so a paid user in an unpaid workspace sees enabled UI but gets a clear 403. Aligning the UI to the active-org plan is a follow-up.

## [3.39.0] - 2026-06-19

### Added
- **Map-reduce summarization for long meetings (behind a toggle).** The completion-pass summarizer fed the whole transcript to one LLM call, capped at ~50K tokens — so meetings beyond ~4–5h were truncated (the summary covered only the first portion, stamped "covers ~X%"). New `MEETING_OPS_SUMMARY_MAPREDUCE` mode (default **off**) chunks long transcripts on **speaker-turn boundaries** (~28K tokens/chunk, ~1.5K overlap — both configurable via `MEETING_OPS_SUMMARY_CHUNK_TOKENS` / `_OVERLAP_TOKENS`), summarizes each chunk into a structured speaker-attributed digest (**map**), then synthesizes the digests into the final notes (**reduce**) — covering the **entire** meeting with every LLM call bounded to one chunk. The reduce emits the **identical** `final_summary` JSON contract (no frontend/downstream change); short meetings (≤ one chunk) use the existing single-call path **byte-for-byte unchanged**; any map-reduce failure falls back to the single call. Map chunk-calls run with bounded concurrency. Stamps `summary_mode` + `summary_chunks` in processing_metadata.

## [3.38.5] - 2026-06-18

### Changed
- **Qwen `enable_thinking=false` is now gated + env-controllable.** Meeting-Ops already disabled the Qwen reasoning step on its LLM calls (a speed win), but sent `chat_template_kwargs.enable_thinking` *unconditionally* — which would 400 against a frontier/non-Qwen endpoint (BYOK). It's now applied only when the target is a local Qwen model (name contains "qwen" + not an external host like api.openai.com / anthropic / openrouter), funnelled through one helper in `LiteLLMProvider._build_payload` (covering summaries, titles, chat, insights, digests, RAG agent). New `MEETING_OPS_DISABLE_THINKING` env flag (default on) toggles it without a code change. No change to the local-Qwen path (still no-think); removes the latent 400 for frontier models.

## [3.38.4] - 2026-06-18

### Fixed
- **Speaker assignment now works on mobile.** The inline assign / rename / merge popover was `absolute`-positioned inside the scrolling transcript (so it rendered off-screen / clipped on phones) and only closed on `mousedown` (ignoring touch). It now renders through a portal with viewport-clamped `fixed` positioning — flipping above the label if it would overflow the bottom — and closes on `pointerdown`, so it's usable on touch devices.
- **Renaming/identifying a speaker now refreshes the summary.** Three per-session speaker operations — `identify-speakers`, speaker-link merge, and re-diarize — updated the transcript's speaker names but left the meeting summary referencing stale labels (e.g. "Speaker 1" after a participant was named after the summary was generated). They now enqueue the existing best-effort, async summary-refresh job so the summary regenerates from the freshly-named transcript. (The transcript itself was already correctly named; this closes the gap for the summary.)

## [3.38.3] - 2026-06-18

### Changed
- **Completed uploads auto-clear from the upload tray.** A finished upload (stage `done`) previously stayed in the bottom-right Uploads tray until manually dismissed; it now removes itself ~12s after processing completes. Failed uploads remain in place so the error (now the real STT reason — see 3.38.2) stays visible for retry, and in-flight uploads are untouched.

## [3.38.2] - 2026-06-18

### Fixed
- **Failed transcriptions now report the real reason instead of "no segments."** When the STT service rejected or errored on an upload — a recording exceeding the transcription service's audio-length limit (HTTP 400), a server error (500), or a timeout — the pipeline surfaced a generic "Transcription produced no segments," hiding the actual cause. The Parakeet provider now carries the service's real error (status + detail) on its result, and the upload pipeline raises that as the job's `error_message`, so the user/operator sees e.g. "Transcription service error 400: audio too long (…)", "Could not reach the transcription service: …", or a timeout note. (Surfaced while debugging a 5.8h upload whose STT failure was masked by the generic message.)

## [3.38.1] - 2026-06-18

### Fixed
- **Large meeting recordings no longer blocked below your plan's limit.** Uploading a multi-GB recording (e.g. a 6.86 GB Jitsi `.webm`) failed even on the Enterprise plan — whose tier cap is 10 GB — because two host-level ceilings sat *below* the paid tiers and silently overrode them: a flat **500 MB** client-side block in the "new meeting from file" dialog, and a **2 GB** server ceiling (`UPLOAD_MAX_FILE_BYTES`) on the customer node / **500 MB** default on dogfood. All are raised to **16 GB**, which sits above the largest tier cap so the per-org tier quota — not an arbitrary host limit — is the source of truth again. The bulk-import per-file ceiling, the code default, and the example env were aligned to 16 GB for the same reason. Chunked/resumable upload and the per-org quota messages are unchanged.

## [3.38.0] - 2026-06-14

### Added
- **Speaker-name suggestions from self-introductions.** When a speaker isn't matched by voiceprint, Meeting-Ops now scans *that speaker's own* utterances for a self-introduction ("Hi, I'm John", "my name is John Smith", "John here") and offers the name as a one-click chip in the naming UI. Suggestion-only: it pre-fills the existing name field and the user still confirms; it never auto-applies, auto-creates, or auto-links. Computed on-read, gated strictly on `speaker_id IS NULL` (voice-matched/named speakers get no suggestions). Deterministic extractor with a conservative Titlecase + denylist false-positive defense (35 backend + frontend tests; 0 must-fix in adversarial review). New `name_suggestions` field on the unassigned-links + speaker-links responses.
- **Meeting-Ops brand identity (pass 1).** Applied the approved Brand-Ops kit (the suite uses per-app identities; MO = navy/gold "comms officer"): the official mark as favicon/app-icon/sidebar logo (replacing the default Vite placeholder), a branded loading screen, navy `#1b3a6b` theme-color, the "Meeting intelligence you self-host." tagline, and Inter + Space Grotesk fonts. The full purple→navy/gold palette swap follows.

## [3.37.1] - 2026-06-14

### Fixed
- **Memo summaries: `executive` is now always a summary sentence, not a name.** The short/single-speaker memo prompt sometimes emitted the speaker's name in the `executive` field; the JSON contract now explicitly requires `executive` to be the 2-4 sentence overview prose (never a name or title).

### Added
- **Summary-styles info popover** on the Session Details "AI Summary" header (ⓘ): explains that the style is auto-selected by length + speaker count — **Memo** (short/solo: concise summary + key points + explicit actions) vs **Minutes** (longer/multi-speaker: full structured minutes with decisions, quotes, open questions, next steps).

## [3.37.0] - 2026-06-14

**Recordings survive a backend restart/crash.** (Investigate → design → implement → adversarial-review swarm.) Trigger: a real ~2-hour always-on recording was lost when the backend was restarted mid-recording — Stop hit `audio_service.stop_recording()`, which only checks *in-memory* state (wiped by the restart), returned "Not recording this session" → **HTTP 500** → a cryptic "reset the connection" dialog → session marked `error`, audio gone.

### Added
- **Durable in-recording persistence (re-enabled).** Non-privacy always-on recordings now stream audio to the server in ~30s chunks *during* recording (the already-built, unit-tested `/api/recordings/sessions/{id}/audio-chunks` → on-disk `full_audio/<idx>` pipeline; `bufferOnly` flipped off, gated on `!useLocalOnly` so **privacy/local-only still streams nothing**). At minute N the server already holds chunks 0..N on the host volume, so a process/container restart leaves the recording recoverable. Chunk upload retries with backoff and never halts the recording on a transient failure.

### Fixed
- **Restart-resilient Stop (the incident fix).** `POST /api/simple/recording-sessions/{id}/stop` no longer 500s / marks `error` when in-memory recorder state is absent. On `stop_recording()==False` it now recovers: finalize from the on-disk always-on chunk dir (→ canonical reprocess), else a half-written WAV on disk, else returns an honest `no_audio` 200 — never a silent total loss. The happy path (live in-memory ffmpeg, e.g. Conference Room) is byte-for-byte unchanged. The cryptic "reset the connection" dead-end is replaced with a clear "recording recovered / processing" message.
- **Duplicate sessions.** `start-always-on` and the `/api/simple` create gain an optional `client_session_key` idempotency guard (60s + active-status + same-org window) so a retried create reuses the row instead of spawning the two-rows-1-second-apart pair.
- Tests: `test_legacy_stop_recovery.py` (7, incl. the centerpiece "chunks on disk + wiped in-memory state → recovers, not 500"); `fullAudioRecorder.test.ts` (6, streaming/retry/privacy-gate). Backend regression sweep + frontend vitest 124/124 green.

### Known limitations (by design, documented)
- Durability is host-volume disk (survives process/container restart), not per-chunk Garage (volume loss not covered — deferred). Up to ~30s in-flight encoder audio can be lost on a hard crash. The legacy `/record` (non-always-on) surface does not yet chunk-stream. Per-chunk files are retained until session delete (cleanup-after-reassembly is a follow-up).

## [3.36.1] - 2026-06-11

**v3.36.0 follow-up polish.** (3-agent swarm + integration pass.)

### Added
- **Duplicate-recording banner on mobile** — `MobileSessionDetails` now shows the same "It looks like {name} also recorded this meeting → View their copy" banner as desktop (it was desktop-only in v3.36.0).

### Fixed
- **The "voice fingerprint updated" toast is now honest.** Naming endpoints return `my_voice_folded` (true / false / absent) and the UI keys off it: a fold skipped by the consistency floor or a missing sample now says so instead of claiming success on any HTTP 200.
- **Browser-live segments no longer carry a fabricated 0.93 confidence** — the placeholder constant (browser model has no per-word probabilities) is stored as NULL; all consumers tolerate it.
- **Native `confirm()` dialogs are gone** — every remaining `window.confirm` call site (9 files, incl. delete/destructive flows) migrated to a promise-based `showConfirm` built on the existing styled `ConfirmModal` (focus-trap, ESC, default-cancel on danger).
- **Test-suite collection interaction fixed** — `test_pooled_embedding.py`'s module-level import split the SQLAlchemy Base registry when collected with DB-touching suites; suites now pass in one pytest invocation.

## [3.36.0] - 2026-06-10

**"My Voice" portable voiceprints + same-meeting duplicate detection.** (4-agent build swarm + adversarial tenancy/privacy review + hardening pass.)

### Added
- **"My Voice" — a portable, account-level self-voiceprint** (`user_voice_profile`, migration 045). Workspace voiceprints stay hard tenant-scoped (no cross-tenant matching, ever); My Voice adds ONE voiceprint that follows *your account* and participates in identification only inside workspaces where you're a member. On a My-Voice match, the workspace's own SpeakerProfile is auto-created/seeded from your portable centroid — so joining a new workspace means you're auto-named from your first meeting, and the workspace library refines locally from there.
  - **"This is me" checkbox** in the speaker-naming flows (assign + create + enroll): explicitly claims the speaker as you (links it to your account, 409 if it belongs to someone else) and folds that meeting's pooled voice sample into your account voiceprint. Every self-naming in every acoustic condition (desk, phone, headset) refines it.
  - **Settings → Privacy → My Voice card**: enrollment status, sample count, and a "Clear my voiceprint" button. `GET/DELETE /api/me/voice` + `POST /api/me/voice/enroll-from-session`.
  - **Privacy hardening (from the adversarial review):** biometric enrollment requires an *explicit* claim — an email match alone never enrolls; a consistency floor (cosine ≥ 0.30 vs your existing centroid) blocks folding someone *else's* voice into your profile (explicit enroll surfaces it as 409); deleting My Voice also strips every org centroid that was seeded from it (org-recorded samples remain that workspace's data — documented); a my-voice match can never steal a same-name SpeakerProfile already linked to another user; user-confirmed labels get zero my-voice side effects; one bad/stale voiceprint row can no longer abort the whole identify pass.
- **Same-meeting duplicate detection (v1: detect + link).** When two workspace members each record the same call, the session detail now shows "It looks like {name} also recorded this meeting" with a link to their copy. Computed on read (same org, different recorder, >50% time overlap of the shorter recording, >60s) — no migration, self-healing, never crosses the tenant wall, and gated to actual org members (external single-meeting collaborators see nothing).
- Tests: `test_my_voice.py` (12), `test_related_sessions.py` (7).

## [3.35.1] - 2026-06-10

### Added
- **"Recorded by" provenance on sessions.** Workspaces are shared — multiple members' recordings sit side by side, and two people can record the *same* call (exactly what happened with Aaron + Shafen's branding call: each got their own copy in their own workspace). Session cards (grid + list) now show a `rec: <name>` badge and the session header shows whose account captured/uploaded the recording. Backend: `recorded_by` on the list payload (batched, no N+1) and detail payload.


## [3.35.0] - 2026-06-10

**Every naming teaches the voiceprint, every pipeline speaks with attribution, and the ranked audit backlog is cleared.** (Core fixes + a 4-agent swarm.)

### Added
- **Confirmed speaker assigns now harvest a voice sample.** Previously only the enroll path fed the voiceprint; a plain "assign to existing speaker" taught the system nothing. Every confirmed assign now pools a sample from that meeting's turn bank into the speaker's running centroid — so naming someone on a phone call teaches phone-them, and recognition genuinely improves with every naming in every acoustic condition.
- **Short recordings / single-speaker memos get a memo-style summary** (summary + key points + explicit actions) instead of padded 7-section minutes with fabricated-looking quotes and "implied" action items. Full-format prompts gained an explicit anti-fabrication rule ("None identified." for empty sections; quote only words in the transcript) and "or implied" was removed from action-item instructions.
- **Long meetings are no longer silently cut off.** The direct Qwen route's transcript cap rose 60K→200K chars (~5h of dense talk; the old cap really covered ~80 min, not the "~3 hours" the comment claimed). When truncation does happen, it's disclosed to the model, reflected in the executive summary, and stamped in `processing_metadata` (`summary_truncated`).
- Tests: `test_summarizer_prompt_routing.py` (4), `test_semantic_search_chunking.py`, `test_unified_agent_providers.py`, `notifications.test.ts`.

### Fixed
- **The record→stop pipeline now identifies + normalizes speakers BEFORE summarizing**, and summarizes via the attributed-prompt path — it was the last pipeline still feeding the LLM flat unattributed text through the legacy `analyze_meeting` (with a stale "Granite 3.3 8B" log line to boot).
- **The registry LLM route gets the full structured summary prompt** — previously it used a stripped executive/bullets-only ask, so quotes/next-steps/questions/minutes were permanently empty on any deploy without the direct env var, and UI sections silently varied between deployments.
- **Speaker-aware chunking handles real names** — the turn-split regex missed "Mary-Jane:", "O'Brien:", "Dr. Smith:", "José:"; now a permissive per-line split. Removed the misaligned dead zip in the overlap path.
- **`/api/agents/providers` no longer reports hardcoded fake availability** (Ollama "available: True # TODO", stale granite3.3 labels) — it derives from the live model config; the canned-transcript `/test` endpoint is gone.

### Removed
- **`meeting_intelligence_real.py`** — unmounted dead router calling raw Ollama (never loaded by main.py).
- **`MeetingIntelligenceDashboard`** — fully unreachable dead component (its only mount sat behind a condition that could never be true) + its dead branch in Sessions.tsx, mock, and stale CLAUDE.md "Known Issues" entry.

### Changed (frontend polish)
- AskBar 404 fallback: dev jargon ("Wire the MCP and it'll light up") → "Ask isn't enabled on this workspace yet."
- `notifications.ts` / `errorHandling.ts` wrappers rewired from `window.alert` to toasts (kills the next alert() regression at the source).
- Room-setup wizard's permanently-disabled "Network audio stream — Coming soon" tile hidden behind a feature flag.
- Stripped ❌-emoji debug prefixes from SessionDetails console.error calls.


## [3.34.0] - 2026-06-10

**Voiceprints that actually match, tenancy-correct enrollment, honest endpoints, real export-email, and label/attribution parity across chat + RAG.** (Core fixes + a 3-agent polish swarm.)

### Fixed
- **Speaker auto-identification now pools several short turns instead of embedding one long mush.** Identify embedded the single LONGEST segment per speaker — on real calls that's a ~5-minute Parakeet merge whose embedding matches nobody (measured 0.05 self-similarity). The diarizer's own fine-grained turns (with embeddings) are now persisted (`transcript_diarized.speaker_turns`, capped) and identify pools up to 5 clean 2-45s turns per speaker (L2-normalized mean). Legacy sessions fall back to the old behavior. Identify also groups by `raw_label` so post-normalization re-identifies still line up, and stale unconfirmed speaker-links from superseded diarizations are pruned (no more phantom speakers in the naming UI).
- **Naming a speaker on another workspace's meeting enrolled the voiceprint into the WRONG workspace.** The session-scoped naming/enroll/assign paths keyed the SpeakerProfile off the caller's ACTIVE org; a superuser naming from god-view (exactly how Aaron labeled Shafen) silently enrolled into their own workspace, where that session's identify can never see it. All three paths now use the session's org.
- **Live meetings finally reach search + RAG without a manual reprocess.** `finalize_session_job` never indexed to Qdrant — always-on meetings were invisible to cross-meeting search/RAG until someone reprocessed, and the v3.33.x rename-refresh left stale labels/titles in the index forever. The job now indexes after summarization (mirrors reprocess Stage 5.9; delete-and-rewrite also heals renames).
- **Per-meeting AI chat now reads attributed transcripts.** `_get_transcript_text` preferred the flat unattributed text, so "who said X" answers were guesses; it now builds "Name: utterance" lines from the diarized segments (normalized), falling back to flat text only when no segments exist.
- **Upload pipeline label parity:** uploads now run label normalization + transcript-row resync after identify (raw `SPEAKER_00` no longer shows in an upload's transcript while its summary says "Speaker 1").

### Added
- **Export-by-email is real now** (was an honest 501): exports are sent via Postmark with the artifact attached (7MB cap), with truthful per-job `emailSent`/`emailError` reporting — never fake success. 501 only when Postmark isn't configured; 422 on malformed addresses.
- **Toast after naming a speaker** ("Summary will update with the new name in about a minute") across desktop + mobile editors and the Speaker Library.
- Tests: `test_pooled_embedding.py` (5), `test_meeting_management_not_implemented.py` (9), `test_batch_export_email.py` (5).

### Changed
- **`meeting_management.py` mock endpoints are now honest HTTP 501s** (fake NPU reprocess/status, in-memory templates, an analytics route reading columns that no longer exist — it 500'd on any real data). Real endpoints (export delegate, delete/update) untouched; router load order pinned by test so `/health` counts stay stable.

### Audit backlog (found, not yet fixed — ranked)
- record→stop path still summarizes flat text before identify (legacy `analyze_meeting`); summarizer prompt: short-memo fabrication risk, silent 60K-char truncation on >~80-min meetings, lighter prompt on the registry route; speaker-aware chunking regex misses hyphenated/accented names; AskBar dev-jargon fallback; `unified_agent_api` hardcoded provider list; dead `meeting_intelligence_real.py`; fabricated 0.93 confidence on browser-live segments; ~14 native confirm() dialogs; MeetingIntelligenceDashboard is fully unreachable dead code (delete next pass).


## [3.33.2] - 2026-06-10

### Fixed
- **The v3.33.1 rename-triggered summary refresh silently never ran.** `finalize_session_job` has a drift-guard that skips any job whose id doesn't match the session's stamped `processing_job_id` — and completed sessions still carry the *stale* id from their original finalize. The rename hook enqueued the job without re-stamping, so every refresh was skipped as "drift" (caught live on the Shafen call, session 510). The hook now stamps the new job id before returning, same as the finalize endpoint does.

## [3.33.1] - 2026-06-10

### Added
- **Naming a speaker now auto-refreshes the summary — no full reprocess needed.** Renaming/enrolling a speaker already rewrote the transcript labels in place; the only stale artifact was the summary (still "Speaker 1"). All three naming paths (assign-existing, inline create, enroll) now enqueue the lightweight finalize job — identify → normalize → summarize → insights, **skipping STT + diarization** — so the summary picks up the new name in ~40-60 s instead of the ~3-4 min full reprocess. The summary's idempotency hash folds speaker names in, so a real rename re-runs the LLM while a no-op skips it. Best-effort: a queue hiccup never fails the rename. (Full reprocess remains the right tool when the *audio/transcript* needs redoing.)

## [3.33.0] - 2026-06-10

**Humane speaker labels everywhere: summaries read attribution instead of guessing it, raw `SPEAKER_00` codes are gone, and the transcript view shows per-line speakers instead of "unknown".** Found via Aaron's phone call with Shafen — the summary said "the homeowners (SPEAKER_00)" and the transcript showed every line as unknown.

### Fixed
- **The summarizer now reads a speaker-attributed transcript instead of guessing who said what.** `_summarize_session` fed the LLM a *flat* `transcript_simple` (no per-line attribution — a stale comment claimed names were folded in, but nothing ever rewrote it) plus a list of labels and an instruction to "attribute statements to them." The model had to guess, and it parroted raw pyannote codes. It now builds the prompt body from the diarized segments as **"Name: utterance" lines** (consecutive same-speaker segments merged) — attribution is read, not inferred — with the prompt forbidding machine codes and identity-guessing for unidentified voices.
- **The live always-on finalize never identified speakers at all.** `finalize_session_job` went straight to the summary; only the *reprocess* pipeline ran `identify_speakers`. So a live meeting was summarized with raw diarizer labels even when the voices were enrolled. The finalize job now runs **identify → contact-stamp → label-normalize → summarize**, matching the reprocess order.
- **Transcript view showed "unknown" for every line.** The `transcriptions` rows the UI renders were inserted *before* diarization overlaid speaker labels, and nothing ever synced them afterwards. The new normalization step re-syncs those rows from the final diarized segments, so the transcript shows the same names/labels as the summary.

### Added
- **`services/speaker_labels.py`** — label normalization: identified voices keep their real name; unmatched diarizer codes become stable **"Speaker 1" / "Speaker 2"** (first-appearance order, idempotent, never collides with existing numbers); the original code is preserved in `raw_label` so the inline naming/enrollment flow (which keys on `raw_label`) is unaffected. Runs in both finalize and reprocess (new Stage 4.5). Covered by `tests/test_speaker_labels.py`.

### Note
- Voice **identification** itself still requires enrollment: naming a speaker once (inline prompt on the session) enrolls their voiceprint for future auto-naming. Phone-bandwidth audio matches less reliably until a phone sample is enrolled — naming one phone call fixes the next one.

## [3.32.3] - 2026-06-09

**Fix: Stripe webhook deliveries were silently auth-walled — paid upgrades would never take effect.**

### Fixed
- **`POST /api/stripe/webhook` was being 302-redirected to the Keycloak login by oauth2-proxy**, so Stripe's webhook deliveries could never reach the handler. The end-to-end consequence: a customer could complete checkout, but the `customer.subscription.created` event would bounce off the login wall → `User.tier` and `Organization.plan` would never update → **they'd pay and not get upgraded** (and the per-org quota would stay capped). Added `^/api/stripe/webhook$` to `OAUTH2_PROXY_SKIP_AUTH_ROUTES` so the endpoint skips the interactive-login layer. This is safe because the handler does **mandatory Stripe signature verification itself** — an unsigned or forged POST is rejected with 400 (verified: good signature → 200, bad → 400). Found while bringing up Stripe test mode (a locally-signed event through the public URL returned 302→login instead of 200). Applies to both test and live keys.

## [3.32.2] - 2026-06-09

**Fix: actually pass `STRIPE_TEST_*` into the containers (v3.32.1 wired the code but not the compose env).**

### Fixed
- **`STRIPE_TEST_MODE` never reached the backend/worker, so v3.32.1's test-mode switch couldn't engage.** The bigboy compose injects Stripe config via an explicit `environment:` list (`- STRIPE_X=${STRIPE_X:-}`), not the whole env file — so dropping `STRIPE_TEST_*` into `.env.bigboy` had no effect; `stripe_client` still resolved the live key and `test_mode()` stayed `False`. Added the full `STRIPE_TEST_*` passthrough (mode flag, test api key, publishable, webhook secret, and all six Basic/Pro/Suite price IDs) to **both** the `backend` and `meet-bulk-import-worker` service env blocks. With the test keys provisioned and `STRIPE_TEST_MODE=1` set, billing now correctly runs against Stripe test (verified: `test_mode=True`, active key `sk_test_`, test prices + webhook secret resolved). Caught during the live test-mode bring-up on dogfood.

## [3.32.1] - 2026-06-09

**A Stripe TEST-mode switch so the upgrade flow can be exercised end-to-end without charging a real card.**

### Added
- **`STRIPE_TEST_MODE` — one flag flips the whole billing subsystem onto Stripe's test environment.** The dogfood node has *live* Stripe keys, so clicking through checkout there would charge a real card — which made the self-serve upgrade flow untestable. Setting `STRIPE_TEST_MODE=1` now transparently redirects every `STRIPE_<X>` config read in `services/stripe_client.py` to its `STRIPE_TEST_<X>` variant (api key, webhook secret, publishable key, and all per-tier price IDs). The live `STRIPE_*` vars stay configured but dormant, so flipping back to production is a one-liner and we never delete the live credentials to test.
  - **Fail-safe:** in test mode the wrapper *refuses* any resolved secret key that isn't an `sk_test_` key (and `is_configured()` reports billing-unavailable to match), so a half-configured node — flag on but `STRIPE_TEST_API_KEY` unset — goes **inert** rather than silently falling back to the live key and charging a real card.
  - **`POST /api/stripe/webhook`** now verifies against the mode-aware signing secret (`STRIPE_TEST_WEBHOOK_SECRET` in test mode), keeping test + live webhook deliveries fully separate.
  - **Pricing page shows a loud amber "🧪 Stripe TEST MODE — checkouts here use test cards … and never charge a real card" banner** when the backend reports test mode (new `test_mode` field on `GET /api/billing/subscription`), so a QA checkout can't be mistaken for a live one.
  - **`backend/scripts/stripe_test_provision.py`** provisions the test environment from a test secret key: idempotently creates the Basic/Pro/Suite monthly+annual prices (reused by Stripe `lookup_key`), creates the webhook endpoint, and prints the ready-to-paste `STRIPE_TEST_*` env block. Refuses to run with anything but an `sk_test_` key.
- **Coverage:** `tests/test_stripe_test_mode.py` (flag parsing, the redirection, the live-mode-unchanged path, and the fail-safe). Also repaired the pre-existing `test_billing_api.py` checkout mocks, which hadn't been updated for the v3.32.0 `organization_id` checkout parameter (the suite was red); they now assert the active org is threaded through.

### Note
- Live billing behavior is unchanged when `STRIPE_TEST_MODE` is unset (the default): every read resolves to the same live vars as before. This release is a no-op for production until the flag is explicitly flipped with test keys in place.

## [3.32.0] - 2026-06-09

**Self-serve upgrade actually lifts the quota, a per-user sticky default workspace, and one consolidated fetch interceptor.**

### Fixed
- **Paying for a plan now lifts the org's quota (self-serve upgrade works end-to-end).** A successful Stripe checkout moved `User.tier` (feature gates) but never `Organization.plan` — which is what the monthly-audio-hours quota reads — so a paying customer still hit the free 10-hour cap (the exact wall that blocked Shafen, just reached via payment instead of manual comp). The active org is now threaded through checkout → the Stripe subscription metadata (`meeting_ops_organization_id`) → the webhook, which sets `Organization.plan` (and clears any stale per-org hour cap) on `subscription.created/updated` and reverts it to free on `deleted`. Tier→plan mapping: enterprise→unlimited, any paid tier (basic/pro/suite)→Pro quota, cancel→free. (Stripe was already fully configured — live keys, webhook secret, price IDs — so this closes the last gap; checkout is now genuinely self-serve.)

### Added
- **Per-user sticky / default workspace.** New `User.default_organization_id` (migration `044`). When a request arrives with no explicit workspace selector, `resolve_active_organization` now prefers the user's saved default org (if they're still a member), then the platform home org, then their first membership — replacing the dogfood-specific `DEFAULT_ORG_SLUG` hack with a per-user choice that works on any node and follows you across devices. Switching workspaces in the UI persists the choice via the new `PUT /api/organizations/default`; the frontend prefers the server default ahead of the local sticky.

### Changed
- **Consolidated the two `window.fetch` interceptors into one.** The active-org header injection (was in `AuthContext`) and the JWT-401 refresh + stale-session handling (`installFetchInterceptor`) were two separate, order-fragile `window.fetch` monkeypatches — if they wrapped in the wrong order the org header could silently fail to ship. They're now a single, idempotent interceptor that attaches the org + auth headers AND handles 401 refresh/retry; `AuthContext` registers its refresh callback with it instead of wrapping fetch a second time. Removes a long-standing source of "the org header didn't make it" flakiness.

## [3.31.0] - 2026-06-09

**Tenancy hardening + an explicit platform-admin god-view, plus long-meeting diarization fixes.**

### Security / tenancy
- **The platform-superuser session list is now privacy-scoped by default.** Previously a superuser's "all my orgs" view returned *every session in the entire system*, so the platform owner saw every tenant's private meetings on the normal Sessions page. It now scopes to the orgs the caller is actually a **member of** — for everyone, superuser or not. (Regular users were already correctly isolated; this only changes the superuser default.) Cross-tenant content access now requires real org membership, a shared workspace, or the explicit opt-in below.
- **New: explicit, opt-in platform-admin "all tenants" god-view.** Superusers get a clearly-labelled **"View all tenants"** toggle on the Sessions page (hidden from everyone else). Off by default; when on, the list crosses every org and the UI shows a loud amber "Platform admin — viewing ALL tenants' meetings" banner. Each use is logged for an audit trail. The backend honors the `all_tenants` flag only for superusers. This replaces the silent, always-on god-view with a deliberate, visible, audited one.

### Fixed
- **Diarization failed on any meeting over 30 minutes.** The speaker service had a hard 30-min (`MAX_AUDIO_SECONDS=1800`) cap — a leftover from when diarization ran on a 12 GB 3060 — so a real 90-min meeting was rejected with "audio too long → diarization returned no segments," landing a summary with no speaker attribution. It now runs on the 24 GB RTX 3090 (measured: a 92-min meeting diarized in 91 s using only 2.6 GB), so the cap is raised to **8 hours**. (A true full-day single recording is better served by always-on auto-split into meetings.)
- **Reprocess couldn't find the audio for upload-origin sessions.** `_run_session_reprocess` always tried to reassemble always-on per-chunk WAVs, so reprocessing any *uploaded* meeting died with `FileNotFoundError` on the missing `always_on/.../full_audio`. Uploaded sessions have a single audio file (`uploads/.../extracted.wav`) at `session.audio_file`; reprocess now uses it directly when there are no chunks to reassemble. This unblocks the full pipeline (diarization + embedding/Qdrant + knowledge-graph) for uploaded meetings.

## [3.30.2] - 2026-06-09

### Fixed
- **Upload quota / plan-limit errors no longer surface as raw JSON.** When an upload hit a plan limit (the HTTP 402 from `/api/uploads/start` — monthly audio hours, file size, or concurrent uploads), the frontend threw the raw response body (`{"detail":{"code":"monthly_hours_exceeded",...}}`) straight into a toast. It now parses the backend's human `detail.message` and shows a clean error. For the genuinely plan-capped cases (monthly hours, file size), it shows a **persistent "Plan limit reached" toast with a "View plans & upgrade →" button** that routes to the pricing page. Covers every upload entry point (Import/Export, drag-drop, the upload tray, and create-with-upload, which all funnel through `UploadsContext`).

### Note
- The monthly-audio-hours quota is enforced **per organization off `Organization.plan`** (`services/quotas.py`), which is separate from the per-user feature tier (`User.tier`). To comp an org, set its `plan` (e.g. `enterprise` → unlimited hours), not just the user's tier.

## [3.30.1] - 2026-06-09

### Fixed
- **"Recording appears stuck" no longer false-positives on ordinary meeting silence.** The always-on recorder flagged a recording as stuck after just **60 seconds** with no captured audio — but a "chunk" is a VAD-detected *speech* slice, so any normal quiet stretch (someone reading a doc, a thinking pause, waiting for people to join) produced no chunks and tripped the destructive "Reset recording state" prompt. Raised the threshold to **15 minutes** of no captured audio, measured from the last speech chunk (or from session start if one never landed, so a silent start is fine too). A genuinely suspended/dead tab leaves a much larger gap and is still caught. The engine-missing desync check is now separate, with a 30-second boot grace. Message reworded from "for over a minute" to "for a while."

## [3.30.0] - 2026-06-08

**Production-ready / pre-launch milestone.** Consolidates the v3.29.6 → v3.29.13 arc (session-card enrichment, the security + robustness pass, the Sessions perf fix + speaker-naming UX, and the pre-onboarding "make everything honest" sweep) into a tagged release, with a docs + Help refresh.

### Changed
- **Help page refreshed.** Named the server summary/chat model — **Qwen 3.6 35B-A3B-Vision** (MoE) — in the operator-tier pipeline description (was "a larger LM"), and added a **"Naming the speakers"** section to the end-user tier covering the new inline prompt (name unidentified voices right from the meeting record; one tap each; teaches the system for future meetings). The role-based structure (everyone / team admins / administrators) and the rest of the content were already accurate.
- **`CLAUDE.md` updated** from the stale "v3.28.4 (v3.29.0 in progress)" to the v3.30.0 milestone, with an explicit **live-inference-stack** block (Parakeet / pyannote 3.1 / Qwen 3.6 / Infinity / Kokoro) so the canonical project doc no longer implies the corrected-away Whisper/Granite/gpt-oss labels.

## [3.29.13] - 2026-06-08

**Two robustness fixes surfaced by the live pre-launch walkthrough.**

### Fixed
- **No more white-screen on an already-open tab during a deploy.** When a new frontend build replaces the hashed JS chunks, a tab that's been open since before the deploy can fail to lazy-load a now-deleted chunk ("Failed to fetch dynamically imported module") and render blank. `main.tsx` now listens for Vite's `vite:preloadError` (and a matching `unhandledrejection`) and **reloads once** to pick up the fresh bundle, with a 10-second guard so it can't loop. A first-time visitor was never affected; this covers existing sessions.
- **Sessions "No sessions found" no longer flashes during load.** When the list hydrates from an empty cache, `loading` is already `false` before the real data arrives, so the empty-state showed for ~1s on a populated account. Gated it behind a `hasFetchedOnce` flag — the skeleton shows until the first server fetch completes, then either the grid or a genuine empty-state. (A brand-new user with no meetings still sees the empty-state immediately.)

## [3.29.12] - 2026-06-08

**Pre-onboarding sweep — every Settings panel + new-user surface made honest.** An 18-agent audit (70 findings, 63 verified) drove this; combined with a live browser walkthrough confirming the real experience. The goal: nothing stale or fake before a real user pokes around.

### Removed
- **The fake "NPU Acceleration" panel in Settings → Performance.** It claimed "AMD Phoenix NPU detected — 220× faster transcription" with fabricated metrics ("0.0045 RTF, 4,789 tok/s") and had a toggle + power-mode + CPU-threads + memory-limit controls that were **wired to nothing**, plus an appliance-only "Reference Hardware" (Ryzen/Hawk Point) block. All removed; replaced with an honest "nothing to configure here — Parakeet 1.1B / pyannote 3.1 / Qwen 3.6 run on the server" note. (Dropped the dead `npuEnabled`/`npuPowerMode`/`cpuThreads`/`memoryLimit` persistence too.)

### Changed
- **Settings → AI is now an honest read-only "AI Engine" panel.** Replaced the misleading model switcher (LLM Provider dropdown with dead Ollama/OpenAI/Custom options + a model list showing the legacy `gpt-oss-20b`/`granite` that don't actually run) with a live status view sourced from `/api/system/pipeline`: Summaries & chat = **Qwen 3.6 35B-A3B-Vision**, Transcription = **Parakeet**, Diarization = **pyannote 3.1**, each with a ready indicator. The real controls (summary format, AI toggles, max speakers) stay.
- **Stale model names corrected everywhere they were user-visible.** `Granite 3.3 2B` / `gpt-oss-20b` → **Qwen 3.6 35B-A3B-Vision** (LiveRecording progressive-summary fallbacks, the Agent Dashboard/Editor copy + presets + model-performance card, the default `llmModel`); lingering `Whisper` copy → **Parakeet** (AudioSettings); internal **"Brigade"** → "Project-Ops, Contact-Ops, and other Unicorn Commander suite apps" in the Integrations description.
- **Login: the SSO button now says "Sign in with Unicorn Commander"** (was "Sign in with SSO / enterprise") — matches the actual identity provider a new user clicks.
- **Share-meeting invite is instance-relative.** The members link was hardcoded to `meetingops.magicunicorn.dev`; now uses `window.location.origin`, and "a unicorncommander.ai account" → "an account on this instance" (so it's correct on any deployment).
- **MeetingIntelligenceDashboard** empty-state no longer says "Generate … to see content" (there is no Generate button) — now passive: "Templates will show content when analysis completes."

### Verified live (browser walkthrough as a new user)
- SSO **auto-signs-in with no password prompt** ("Sign in with Unicorn Commander"); AI Chat answers in ~8s on the Qwen 3.6 agent; the Sessions list loads and the pipeline-status panel reports the real models; no console errors on load. (Left as a minor follow-up: the Sessions "empty" state flashes ~1s during load for users who already have sessions — correct + instant for a brand-new user.)

## [3.29.11] - 2026-06-08

**Settings now reflect the real stack (Parakeet, not Whisper), and speaker avatars are gone.**

### Changed
- **Settings → Audio showed a stale Whisper model picker.** Replaced the "Transcription Model" dropdown (Whisper base…large-v3 + "NPU 220x") — none of which is used — with an accurate read-only **Transcription Engine** panel: live in-browser **Parakeet** (WebGPU, on-device, $0) + server **Parakeet 1.1B** with **pyannote 3.1** diarization and a Qwen 3.6 summary. Also fixed the appliance-device hint ("on-appliance Whisper pipeline" → "transcription pipeline (Parakeet)").
- **Live-recording model labels no longer flash stale values.** The status pills defaulted to "Whisper large-v3-turbo" / "Granite 3.3 2B" before `/api/system/pipeline` resolved; the defaults are now "Parakeet 1.1B" / "Qwen 3.6 35B-A3B-Vision" (the live endpoint already reported the correct labels — this just fixes the initial render).
- **Removed speaker avatars from the session cards.** The avatar circles (mostly initial-letter placeholders, since few speakers have photos) were visual noise and added render cost; the cards now show **speaker names only** (desktop + mobile). The diarized-speaker info stays; just the pictures are gone.

### Notes
- Confirmed the AI Chat agent already runs **Qwen 3.6 35B-A3B-Vision on midboy1's P40** (`llm-gateway:8088`) for every org — no change needed. (A separate legacy model-registry drives the Settings → AI *switcher* display and still shows `gpt-oss-20b`; that surface is cosmetic/superseded by the env-driven provider and can be reconciled separately.)

## [3.29.10] - 2026-06-08

**Sessions-list speed fix + speaker-naming made frictionless + a page-size control.**

### Performance
- **Fixed the slow Sessions list (a regression from v3.29.6).** The list endpoint resolved named speakers with a **per-session query** (`_speakers_payload`) inside the response loop — an N+1 that ran up to ~100 extra speaker lookups per page, which is what made the cards visibly slow. Replaced it with a single batched query for the whole page (`_speakers_by_session_batch`, one `IN (...)` join → `{session_id: [...]}`). N+1 → 2 queries; same card data.

### Added
- **Speaker naming is now a prominent, in-place prompt when speakers aren't identified.** On a meeting whose speakers are still raw diarization labels (`SPEAKER_00` / "Speaker 1"), the in-record Speakers card now turns into an **amber call-to-action** — "N of M speakers still need a name — tap to assign" — auto-expanded with the assignment dropdowns right there (each pre-filled with the diarization best-guess, usually one tap). Once everyone's named, it goes back to the quiet "Speakers (N)" card. The goal: name speakers in the fewest clicks, right where you read the meeting, instead of hunting for the Speakers tab.
- **Per-page control on the Sessions list.** A "Showing X of Y · Per page [12 / 25 / 50 / All]" selector caps how many cards render at once (default 25). Rendering all ~100 avatar-bearing cards at once was part of the sluggishness; this keeps the DOM light while still letting you show everything on demand. Grid, list, and mobile views + select-all all respect the cap.

## [3.29.9] - 2026-06-08

**Completeness-audit wave 4: production-readiness backlog.** An 8-group investigation swarm authored + adversarially verified precise fix specs; this ships the vetted set (dead-UI removal, integration robustness, a real cross-org delete fix, auth hygiene). Two findings were intentionally held: one whose marker has no consumer yet (Brigade failure-stamp — failures are already ERROR-logged), and one unsafe "unmount" recommendation (the `meeting_management` router still uniquely serves a live, test-covered export endpoint).

### Fixed
- **Rooms "Discard recording" hit a NON-org-scoped delete handler.** `roomsApi.discardSession` called `DELETE /api/recording-sessions/{id}` (served by `meeting_management.py`, not org-scoped) instead of the canonical org-scoped `DELETE /api/simple/recording-sessions/{id}` (`_get_session_for_org`). Repointed — a stray cross-org id can no longer be deleted via this path.
- **Agent create/import/instantiate stamped a random `created_by`.** Three superuser endpoints in `agent_management_api` used `uuid.uuid4()` placeholders (TODO) instead of the authenticated caller; now derive a deterministic UUID from `current_user.id`, so ownership is real and stable.
- **RAG/AI-Chat sources now show a snippet.** The search snippet the backend already produced was dropped before reaching the UI; it's now carried through `meeting_rag` sources and rendered (2-line, muted) under each citation in RAGChat.

### Changed
- **Known Issue #1 cleanup (MeetingIntelligenceDashboard).** Removed the dead live-notes WebSocket connect (it pointed at `/api/meeting-intelligence/ws/live-notes/{id}`, which the backend never implemented, so it errored + spammed the console on every recording render) and the two orphaned no-op functions (`generateLiveNotes`/`generateFinalReport`, zero call sites). The canonical summary still loads via the existing fetch path; ~110 lines of dead code removed.
- **Project-Ops push failures are now visible instead of silent.** The three callers of `submit_action_items_to_triage` (live finalize, reprocess, Arq worker) discarded the typed result; they now log a warning when a push doesn't succeed (the most likely prod failure — a per-process KC service-token mint — was previously invisible). A failed triage push also stamps a `po_push_failed_at` marker on the session (`processing_metadata`) so a backfill can find it, cleared on the next success. All best-effort — finalize never raises.
- **Batch-export email is now an honest 501.** `POST /api/export/batch` with `emailTo` set silently accepted a job it would never email; it now returns 501 up front (and the worker path logs loudly if reached). No dead "email" affordance ships.
- **Contact-Ops resolver failure modes are diagnosable.** It now logs (still returning safely) when: a client-credentials mint comes back missing the requested `contacts:read` scope (the KC client likely doesn't grant it → downstream 401/403); a 200 carries a non-JSON body; or the MCP result shape drifts from what's parsed.
- **Project-Ops client robustness.** The project-resolution pagination loop gained a no-progress circuit breaker (a misbehaving PO backend returning full pages with no new ids can't burn the whole page budget inside finalize); and an env-gated (`PROJECTOPS_EXPECTED_SCOPE`, no-op when unset) one-time warning fires if the token lacks the expected write scope/role.
- **`meeting_management` auth hygiene.** Replaced the `Depends(x) if x else None` conditional-dependency smell on 14 parameters with unconditional `Depends(...)` (the imports are unconditional, so there was no live bypass — but the pattern is now correct).

## [3.29.8] - 2026-06-08

**Completeness-audit wave 3: the "clear polish" — no-decision UI consistency fixes (frontend only).**

### Added
- **Mobile session cards now show named-speaker avatars too** (parity with the v3.29.6 desktop cards). `MobileSessionsList` renders the same overlapping avatar stack (local `SpeakerProfile.photo_url`, initial-letter fallback) + names from the session `speakers` payload, after the summary snippet.

### Changed
- **RAG source labels are now consistent across all three surfaces.** A source with no title rendered three different ways — `Untitled` (Sessions ask panel), `Meeting {id}` / `Source` (RAGChat), `Meeting {id-slice}` / index (dashboard AskBar). All now fall back to **"Untitled meeting"** (titles + real links unchanged).
- **Local (privacy-mode) session cards no longer look empty.** A device-only session with no summary now shows "Live transcript only — no summary on this device" instead of a blank body.

### Fixed
- **RAG relevance score now has an accessible label.** The bare `(NN%)` on Sessions ask-panel sources gained a `title`/`aria-label` ("NN% relevance match") so it's not an unexplained number.
- **RAG follow-up box no longer disappears mid-ask.** It was hidden until `ragMessages.length > 0`, so during the *first* answer (when the list is briefly empty) it vanished; it now also renders while loading (disabled), so the input stays put.

## [3.29.7] - 2026-06-08

**Completeness-audit wave 2: a real tenancy-isolation hardening + a UX-consistency polish pass.** A multi-agent audit swept session-views / incomplete-features / integrations / workspace-tenancy / backend-completeness; this ships the high-value, low-risk confirmed findings. (The audit also *confirmed* two security designs are solid — knowledge-graph cross-org node hydration gating, and the v3.29.3 WebSocket handshake org-scoping — no change needed there.)

### Security / moat
- **Brigade tenancy now fails closed on a misconfigured mode (was: silent cross-org commingling).** `brigade_writer._resolve_tenancy` fell back to `shared` on an *unknown* `BRIGADE_TENANCY_MODE` with only a WARN log — so an operator typo (e.g. `per_org_graphs` for `per_org_graph`) silently wrote **every org's** meetings into one shared knowledge graph, the exact tenancy violation per-org mode exists to prevent. The **write path** now raises on an invalid mode (`strict=True`); the writer's top-level `except` turns that into a logged, skipped write (no data, no commingling) until the env is fixed. **Read/deep-link paths keep the safe fallback** (`strict=False`) — they're downstream org-scoped (KG node hydration filters by `organization_id`), so a wrong graph name degrades to "your own data / empty", and a hard 500 on a read would be worse. An *unset* env is unaffected (it resolves to the valid default).
- **`/api/system-info` is now auth-gated.** It leaked host details (platform, Python version, CPU/memory, NPU driver) without authentication. On the cloud node oauth2-proxy already fronted it; this closes the gap on a self-hosted node with no proxy (same posture as the v3.29.3 WS-auth work). `/api/status` stays intentionally public for container health checks. (The endpoint isn't consumed by the frontend, so no UX impact.)

### Changed
- **All blocking `alert()` calls on the core pages are now themed toasts.** 29 native `window.alert()` popups across `SessionDetails`, `Sessions`, `SessionCreator`, and `LiveRecording` — including the mid-recording "connection lost" alert that froze the page at the worst moment — now use the app's existing `react-toastify` toast system (`showToast.{success,error,warning,info}` / `toast.*`): success/failure/validation mapped by intent. Consistent, non-blocking feedback instead of a browser-chrome modal.
- **Grid-card "AI Summary" preview uses the full summary fallback chain.** The card only rendered the legacy `summary.analysis.executive`; it now matches `SessionDetails` (`final_summary.executive` → `summary.executive` → `summary.analysis.executive`), so new-style summaries show on the card instead of silently dropping out.

### Fixed
- **Clipboard actions no longer claim success on a failed copy.** Three "copy" paths called `navigator.clipboard.writeText(...)` without awaiting/catching, then unconditionally showed success — so a denied/insecure-context write still flashed "copied": the summary-email Copy button (`SessionDetails`), the live "Copy Action Items" button (`LiveRecording`, previously *no* feedback at all), and the share-link "Copied" chip (`SessionPermissionsModal`). All now confirm only on resolve and surface an error on reject.
- **Digests page no longer renders blank before first generate.** Added an initial empty-state ("No digest yet — pick a period and click Generate…") for the `!digest && !loading && !error` branch that previously left the area below the controls empty.

## [3.29.6] - 2026-06-08

**Session-card enrichment + a polish/completeness pass (first wave of the "get to 100%" audit).**

### Added
- **Session cards now show who was in the meeting, with faces.** The Sessions grid cards surfaced participants + a summary snippet, but not the *diarized, named speakers*. Cards now render a **speaker row with avatars** — each named speaker's photo (small overlapping circles, initial-letter fallback) plus their names — sourced from the new `speakers` field on the session-list payload. The summary preview was bumped from 2 to 3 lines so there's "a little more of the summary" at a glance. Avatars come from the **MO-local `SpeakerProfile.photo_url`** (the same photo the Knowledge Graph uses), so the list stays cheap — no live Contact-Ops call per card.
  - Backend: new `SpeakerSummary` model + `_speakers_payload(db, session)` helper (one query per session: confirmed `SpeakerSessionLink` → org `SpeakerProfile`, distinct by name, capped at 12, best-effort/never raises) wired into the `GET /recording-sessions` list response as `speakers: [{name, photo_url, raw_label}]`.

### Fixed
- **RAG / AI-Chat citations were rendering "Meeting undefined chunk undefined: undefined" with dead links.** The `RAGChat` `Citation` type expected `{meeting_id, chunk_idx, snippet}`, but the agent backend (`meeting_rag._sources_from_history`) actually streams `{session_id, title, created_at}` — so every source label was `undefined` and every "Sources" link pointed at `#/sessions/undefined`. Citations now show the **meeting title + date** and link to the real session; falls back to "Meeting {id}" only if a title is missing. (Found by the completeness audit.)
- **"Copy email" reported success even when the copy failed.** The summary-email Copy button called `navigator.clipboard.writeText(...)` with no `await`/`.catch` and then unconditionally `alert('Email copied to clipboard!')` — so a denied/insecure-context write still told the user it worked. It now awaits the write and shows a themed `toast.success` only on resolve, `toast.error` on failure.

### Removed
- **Dead frontend code.** Deleted `MoveSessionOrgModal.tsx` (211 lines, never imported — superseded by the inline org-move dropdown in `SessionDetails` since v3.24) and removed the unreachable `/admin/agents-old` route + the legacy `AgentConfiguration` page (851 lines, ~33 KB, eagerly bundled, no nav link — superseded by `AgentDashboard`). Both verified to have zero remaining references; trims the main bundle.

## [3.29.5] - 2026-06-08

### Changed
- **Speaker roster surfaced back in the session record.** The per-speaker assignment UI (`SpeakerTagger`: each `SPEAKER_xx` with a dropdown to pick an existing person / add to training / create new, pre-selected for identified speakers) was only reachable under the "Speakers" tab. It now also renders as a **collapsible "Speakers" card at the top of the record** on every other view, so you can name/assign speakers inline when you open a session (matching the older in-record placement). Mutually exclusive with the Speakers tab so `SpeakerTagger` never double-mounts. (The empty-roster symptom itself was the org-default bug fixed in v3.29.4.)

## [3.29.4] - 2026-06-08

### Fixed
- **Multi-org users were silently dropped into an arbitrary (often empty) workspace.** When a Meeting-Ops API request reached the backend without an explicit workspace selector (`X-MeetingOps-Org` header / `?org=`), `resolve_active_organization` fell back to `organizations[0]` — the user's *first* membership, which can be an empty/test org. For a user whose first membership is e.g. "Test Co", that meant no speakers/sessions/contacts showed even though their real data lives in their home org — and switching workspaces appeared to "do nothing" whenever the header didn't ride along. The no-selector fallback now **prefers the platform home org (`DEFAULT_ORG_SLUG`) when the user is a member of it**, falling back to the first membership only for users who aren't (preserving the no-403 behavior for non-home-org users like GFL/Shafen). Part of the workspace-selection hardening; the frontend still always overrides via the header/selector when present.

## [3.29.3] - 2026-06-08

**Closes the last security item from the v3.29.0 audit: the live meeting WebSockets now authenticate at the handshake.** This was the deferred "self-host without oauth2-proxy" gap.

### Security / moat
- **WebSocket handshake auth + org-scoping.** `/ws/transcription/{id}` (could push audio for server STT + replay a session's transcription), `/ws/transcription-auto/{id}` (the live-recording socket — live transcription + progressive summaries), and `/ws/audio-levels` accepted **any** connection at the app layer. On the cloud node oauth2-proxy fronts them, but on a self-hosted node with no proxy that was a cross-tenant read + ungated-compute hole. Added a `?token=` JWT check at the handshake (the browser WebSocket API can't set headers), reusing the proven `decode_token` + `_org_ids` pattern already used by `websocket_remote_audio` / `streaming`; the two session-bound sockets are **org-scoped** (a session belonging to another org → close 1008). New shared helper `auth/ws_auth.py`. A **`WS_REQUIRE_AUTH` kill-switch** (default on) allows instant rollback to the open behaviour without a redeploy. The frontend (`LiveRecording.tsx`) now appends the access token to its socket URLs via a `appendWsToken` helper. `/ws/sessions/{id}/live` (server-live) was already authenticated; the vestigial plain `/ws/transcription/{id}` (no frontend consumer) is secured anyway.

### Tests
- `test_ws_auth.py`: reject-without-token (all three sockets), accept-with-valid-token, cross-org session rejection, and the kill-switch path — driven through Starlette's TestClient.

## [3.29.2] - 2026-06-08

**Three more v3.29.0 deferred items closed: a real search-quality fix on the primary recording path, stop/always-on defense-in-depth gates, and a dashboard empty-state cleanup.**

### Fixed
- **Primary record→stop path embedded speaker labels, not names.** `simple_recording_db.process_recording` (the main record→stop completion pass) called `semantic_search.index_session` **before** `identify_speakers`, so the vector copy of every normal meeting carried raw `SPEAKER_xx` labels instead of real names — degrading cross-meeting search/RAG attribution until a reprocess healed it — even though the inline comment said "rewrite labels BEFORE embedding". Swapped the order (identify → index) and now build the indexed transcript **from the speaker-named diarized segments** (`"{speaker}: {text}"`), mirroring the reprocess Stage-5.9 shape. (The "legacy finalize ordering" deferred item turned out to be this primary path, not a dead one.)
- **MeetingIntelligenceDashboard empty state pointed at a removed action.** When a session had no analysis yet, the panel told users to *Click "Analyze Session"* — but that control was removed and the generate functions are no-ops. Replaced with an honest, loading-aware empty state ("Loading session analysis…" / "No analysis available for this session yet" / "The summary is generated automatically when processing completes."). Also corrected the stale `/api/meeting-notes` TODO comment — the dashboard already reads the canonical session `summary` and degrades gracefully on fetch failure.

### Security / moat
- **Defense-in-depth tier gates on two server-compute entry points.** `POST /api/simple/recording-sessions/{id}/stop` (schedules the full transcribe+diarize+identify+index pass) and `POST /api/simple/always-on/start` (server-side continuous capture) were ungated. Both now gate `canonical_reprocess`. `/start` was already gated so `/stop` was naturally protected, but this makes the server-compute boundary explicit and closes the direct-`/stop`-without-`/start` vector. `gate_feature_for_caller` bypasses the internal room-recorder token, so Conference Room is unaffected; the gates are non-invasive front-checks (paid/internal pass straight through). `/always-on/stop` is intentionally left ungated — it's pure cleanup (no compute). The always-on retroactive-session **org-scoping** refactor stays deferred to a desktop/USB-mic test pass.

### Tests
- Source-order guard pinning identify-before-index in `process_recording` (the service-level behavior is already pinned for reprocess by `test_reprocess_indexing`); free-vs-pro gate tests for `/stop` and `/always-on/start` added to `test_free_tier_enforcement.py`.

## [3.29.1] - 2026-06-08

**Two of the v3.29.0 deferred items, closed: redundant re-summarization is skipped, and reprocess de-dup is made robust.** Both backend-only; no API or UI change.

### Changed
- **Summary idempotency guard.** `_summarize_session` (run at finalize AND on every reprocess) now hashes the *exact* LLM input — system + body prompt, which fold in the transcript, speaker labels, template, and route — and **skips the LLM call when it matches the input that produced the current stored summary**. A reprocess whose re-transcription + diarization came out byte-identical no longer burns a redundant Qwen pass. It still re-runs whenever the transcript, speaker names, or template actually change (those shift the hash), and a new `force=True` always regenerates. The browser-live → server-final progression is unaffected (those inputs differ, so both run) — this only removes provably-identical repeats.
- **Reprocess in-flight lock replaces the stable-`_job_id` dedup.** The old `_job_id="reprocess-{pk}"` prevented concurrent double-runs but also (a) let Arq's `keep_result` window silently swallow a *legitimate* later reprocess of the same session, and (b) could leave a stale job key that blocked crash-recovery's re-enqueue. `enqueue_reprocess` now takes an explicit per-session Redis lock (`NX` + TTL, default `REPROCESS_LOCK_TTL_SECONDS=7200`) and uses a **unique per-attempt job_id**: a duplicate enqueue while one is in flight is a clean no-op, the job releases its own lock (compare-and-delete by token) on finish, the lock self-frees via TTL if a worker dies, and `recover_reprocess_jobs` force-clears stale locks so recovery always proceeds. Added focused tests for both seams (`test_summary_idempotency.py`, `test_reprocess_lock.py`).

## [3.29.0] - 2026-06-08

**Production-readiness pass: moat/security hardening, a silent data-integrity fix, and diarization-on-the-3090 made permanent.** Driven by a multi-agent audit of the whole app (30 confirmed, adversarially-verified findings).

### Security / moat
- **`websocket_auto_summary` router was unauthenticated** — `POST /api/auto-summary/control/{id}?action=force` ran the server LLM on *any* session id and returned that meeting's summary (cross-tenant read + ungated compute), and `PUT/GET /api/auto-summary/settings` let anyone mutate instance-wide summarization settings. Now requires auth, **org-scopes the session**, **tier-gates** the LLM-running `force` action (`qwen36_summary`), and restricts the settings write to superusers. (It also no longer swallows 4xx into 500s.)
- **Closed three ungated server-compute paths** that let Free/Basic tiers run paid GPU work: the **vocal-summary + podcast TTS** endpoints (`/tts/summary`, `/tts/podcast`, both `/start` jobs — now gate `qwen36_summary`), the **"Summarize now"** slice endpoint (`/sessions/{id}/summary-slices` — gates `qwen36_summary`), and the **upload reprocess + attach-upload** paths (`/uploads/sessions/{id}/reprocess` and `start_upload action="attach"` — now gate `canonical_reprocess` / `audio_upload`, matching the transcribe branch).

### Fixed
- **Interactive uploads were silently invisible to search + RAG.** The file-picker/drag-drop upload pipeline (`run_upload_pipeline`) produced a transcript + summary but **never indexed to Qdrant** — the same silent-failure class as the always-on path fixed in v3.28.0, but for the upload door. Added the indexing stage (mirrors reprocess "Stage 5.9": built from diarized segments after `identify_speakers`, best-effort/non-fatal) so uploaded meetings appear in semantic search + cross-meeting RAG.
- **Stale red test** (`test_brigade_graph_endpoint`) asserted `graph_url is not None` after the deep-link was intentionally removed — corrected. Forward-ported four tests off the removed-on-3.14 `get_event_loop().run_until_complete()` to `asyncio.run()` (the suite no longer hangs on newer interpreters).
- **`meet-speaker-svc` blocked its event loop** — the pyannote pass now runs via `asyncio.to_thread`, so the single worker stays responsive (health probe + queued request) during a long diarization.
- Frontend hardening: `KnowledgeGraph` page no longer white-screens on a malformed graph payload; removed a dead "Analyze (Coming Soon)" control; the Dashboard "Ask" placeholder no longer shows a developer-facing string.

### Changed
- **Diarization on the RTX 3090 is now permanent IaC.** Restored the `meet-speaker-svc` service to the bigboy compose (GPU 0, `speaker_models` volume, `HUGGINGFACE_TOKEN`, `/health` healthcheck, `restart: unless-stopped`) and repointed the `SPEAKER_SVC_URL` default (backend + worker) to `http://meet-speaker-svc:8889` — it was running as a hand-started container with nothing in version control. A new compose-parity test asserts any `meet-*` model-service URL has a matching service. Documented the `HUGGINGFACE_TOKEN` diarization prerequisite in the env example.
- **Knowledge Graph is GA** — the compose build default `VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED` flips to `true` (was ship-dark) to match the v3.28.0 release notes; the flag stays as a kill-switch.
- Removed the last user-facing **"Brigade"** wording leaks (SessionDetails upgrade card + expanded graph section, Help integration card) — the product term is "knowledge graph".
- Added regression coverage for the Stage-5.9 indexing seam and the `meet-speaker-svc` diarize provider; refreshed the stale `CLAUDE.md` / `backend/CLAUDE.md` stack docs (Qwen 3.6 / LiteLLM / Infinity / pyannote-on-3090) and added a measured throughput benchmark (`docs/throughput-benchmark-2026-06-08.md`).

## [3.28.4] - 2026-06-07

### Added
- **Sessions: one-click "Select all (N) / None".** Added an always-visible select-all / clear-selection control next to the view-mode toggle that works in **both** the grid (default) and list views. Previously only the list view's table header had a "select all visible" checkbox, so in the card/grid view you had to tick each card by hand; the new control selects (or clears) every session in the current filtered list in one click. ("None" is disabled when nothing is selected.)

## [3.28.3] - 2026-06-07

### Changed
- **Removed all user-facing "Brigade" references from the Knowledge Graph UI.** Brigade is internal infrastructure — the knowledge graph lives *in the app* (sidebar). The inline per-session 3D viewer's loading/error/empty copy now reads "knowledge graph" (was *"This meeting hasn't been synced to Brigade yet"* / *"Loading graph from Brigade…"* / *"Brigade is unreachable"*); the per-session `brigade-graph` endpoint no longer emits an "Open in Brigade" deep-link (so the inline viewer footer just shows the node/edge count); and the Pricing feature labels drop "Brigade" ("Knowledge graph (cross-meeting)" / "Knowledge graph + cross-app federation").

### Notes
- Parked plan: `docs/inference-gateway-and-uc-rollout-2026-06-07.md` — the `unicorncommander.ai` inference gateway (local-first via bigboy GPUs + automatic 3rd-party fallback) that unblocks the customer-node rollout (centerdeep currently can't reach its GPU services / Qdrant).

## [3.28.2] - 2026-06-07

### Added
- **Vocal summary (Kokoro · AF Heart).** The session "Listen" panel is now a Vocal summary: Qwen 3.6 writes a SPOKEN-style narration — conversational prose shaped for the ear, distinct from the written summary — cached on the session, and Kokoro TTS reads it aloud in the **AF Heart** voice (defaults to `af_heart`, always renders via Kokoro independent of the org's podcast provider). Kokoro lives on midboy2 (`192.168.10.14:8880`); the `render_tts_summary_job` points there. (The customer node will need its own Kokoro reachability before this works there — midboy2 is LAN-local to the bigboy cluster.)

### Changed
- **Per-session "Open in Brigade" → "View in Knowledge Graph"** — the link on a meeting now opens the in-app Knowledge Graph (`/knowledge-graph`) instead of Brigade (internal infra that wasn't a customer destination).
- **Podcast summary hidden** — the two-voice podcast-recap UI is gated off (code kept, just not rendered). Future: a separate charge or an export to Podcast-Ops.

## [3.28.1] - 2026-06-07

### Added
- **Sessions card view: date / group dividers.** The grid view now renders a section header before each group as you scroll — by **day** when sorted by date (Today / Yesterday / weekday / full date), by **speaker-count bucket** when sorted by speakers, and by **status** when sorted by status (name/duration stay a flat list). Completes the Sessions list-grouping work (the summary snippet + refresh controls shipped in 3.28.0). The table view keeps its sortable Date column.

## [3.28.0] - 2026-06-07

**The Knowledge Graph becomes a real, person-centric feature; cross-meeting search + RAG are made whole; and embeddings + reranking move onto the shared Infinity server (suite-consistent).** Built on v3.27.0 the same day.

### Added
- **Knowledge Graph — person-centric, enabled, hydrated.** The `/knowledge-graph` page (previously ship-dark) is on by default and renders a real connected web of how a person relates across meetings. New `GET /api/knowledge-graph/person/{speaker_id}` + `/person/me`: resolves the speaker org-scoped, fetches their Brigade neighborhood, **expands each meeting neighbor in parallel and merges edges** so 2-hop nodes (topics/decisions/co-speakers) connect instead of floating (~60% floated before), and **hydrates every node from Postgres** (real titles / action-item text / dates / status / deep-links — Brigade stores only id+type) so the graph reads in plain English and stays fresh after edits. Default-loads the viewer's OWN graph (resolve by `linked_user_id` → email → display name, with a featured-person fallback — never a blank canvas). Click a node for a per-type detail card (Meeting → date/length/status + "Open meeting" deep-link to its session; ActionItem → text/owner/status; Decision/Topic → text; Person → avatar + role/company + "view their graph"), a **Back** button to walk person→person, and person **avatars** (Meeting-Ops `speaker.photo_url` override → Contact-Ops federated photo → gradient initials; migration `043` adds the column). Pure render, no LLM — `$0` marginal compute.
- **Help page — role-based tiers.** Restructured into End-user / Team-admin / Administrator sections via a segmented control (defaults to End-user), each a progressively deeper read.
- **Sessions — Refresh button + Auto-refresh toggle** so a new/just-processed meeting appears without a manual reload (the 10s poll is now user-controllable); the **card/grid view shows the meeting summary snippet**, matching the table. (Follow-on: date/sort group dividers.)

### Fixed
- **Recent meetings were invisible to search + cross-meeting RAG.** The reprocess pipeline (every always-on finalize AND every upload) produced a transcript + diarization + summary but **never called the Qdrant index step** — only the legacy recording path did — so 100+ meetings had zero search points despite perfect transcripts. Added a semantic-index stage to `_run_session_reprocess` (speaker-aware, built from the diarized segments), and **backfilled all 207 completed meetings**. New meetings self-index. (Embeddings run on a model, not the diarizer's GPU — the "no Infinity activity" observed on upload was the missing index call, now fixed.)
- **Agent chat surfaced "Source 'brigade' failed: /api/v1/agents returned HTTP 401".** The Brigade agent source forwarded the user's JWT to a service-authed endpoint; it now sends the `X-API-Key` service key (like `brigade_client`/`brigade_writer`) and **degrades silently** to "no Brigade agents" on any failure, so a Brigade hiccup never dumps an error into the chat agent picker.
- **"Open in Brigade" removed from the Knowledge Graph** — Brigade is internal infrastructure, not a customer destination, and its UI didn't honor the deep-link params anyway. (The per-session viewer footer now shows the node/edge count regardless of a graph_url.)

### Changed
- **Embeddings + reranking now run on the shared Infinity server (suite-consistent), replacing the local in-backend fastembed model.** Dense embeddings use Infinity `BAAI/bge-m3` (1024-dim); the RAG query path reranks the RRF candidates with Infinity's `bge-reranker-v2-m3` before building the LLM context; sparse BM25 stays local (cheap). The Qdrant vector dimension is PROBED from Infinity at collection-create time so a model swap can't desync the collection; the collection was recreated at 1024-dim and all 207 meetings reindexed.

## [3.27.0] - 2026-06-07

**Reliability + durability hardening — the cut that makes the dogfood build customer-ready.** Closes the broken-since-June-5 server reprocess pipeline (4 compounding Arq bugs: Redis db mismatch, reserved `_job_id`, missing `models_rooms` import, missing worker model-service env), stops the session watchdog from deleting active OR recoverable recordings, gates the empty-session hard-delete behind a 24h age window (the local-then-upload data-loss landmine that lost session 501), locks the workspace switcher during recording, adds a worker↔backend env-parity test so the reprocess env can't silently drift again, and ships the person-centric Knowledge Graph page (ship-dark). First tagged release since v3.23.3 — all the v3.24.x–v3.26.x work (record-first UX, local-then-upload recorder, WebGPU mobile capture + durability, bulk import/export, weekly digests, PostHog analytics) ships under this tag.

### Fixed
- **Watchdog could still hard-delete a RECOVERABLE recording (local-then-upload data-loss landmine — session 501)** [v3.27.0] — the watchdog hard-deletes "empty" always-on sessions (no server audio AND no server transcript) so the session list doesn't fill with empty `failed` tombstones. But under the v3.26.9 local-then-upload model a desktop recording is empty *server-side* for its entire duration (the audio is buffered in browser IndexedDB until Stop), so an in-flight or briefly-offline session is indistinguishable from an abandoned zombie — deleting its row 404s the orphan-banner resume-upload and loses the meeting. The hard-delete is now gated on row age (`_empty_alwayson_safe_to_delete`): only empties **older than `SESSION_WATCHDOG_EMPTY_DELETE_MINUTES` (24h default)** are deleted; anything newer flips to `failed` so the row survives and the browser can still re-upload it. The heartbeat fix below already keeps an *active* recording from ever being reaped; this closes the second layer (a recording whose heartbeat stalled — laptop sleep, etc. — but whose audio is still in the browser). New unit test `tests/test_session_watchdog_empty_delete.py` (8 cases).
- **Workspace switcher was live during recording** [v3.27.0] — the org switcher in the top bar is now disabled while a recording is in progress (recording / paused / starting / stopping), so a mid-recording org switch can't muddy the session's org binding. (The recorder's `start()` was already idempotent against a double-start; this removes the remaining footgun.)
- **Worker/backend model-service env could silently drift again** [v3.27.0] — added `tests/test_compose_env_parity.py`, which parses the bigboy compose and fails if `meet-bulk-import-worker` is missing any model-service env var (`PARAKEET|SORTFORMER|SPEAKER|EMBEDDING|INFINITY|RERANKER|LLM_MODEL|MEETING_OPS_*|OPENAI|QDRANT|GARAGE`) the `backend` declares. It immediately caught 4 vars missed by the original hotfix (`INFINITY_ENDPOINT`, `INFINITY_RERANKER_ENDPOINT`, `MEETING_OPS_SUMMARIZER_URL`, `OPENAI_API_KEY`), now added — resolving the follow-up noted on the reprocess-worker-env fix below.
- **Always-on recordings over ~30 min were hard-deleted mid-session by the watchdog** [hotfix on v3.26.15] — the session watchdog reaps `status='recording'` rows whose `updated_at` is older than the idle threshold, relying (per its own comment) on chunk uploads bumping `updated_at` every ~30s. But v3.26.9's local-then-upload model stopped streaming chunks during recording, so `updated_at` went stale during an ACTIVE recording and the watchdog **hard-deleted the live session** at the 30-min mark — a real recording (db 501) was lost this way. Fix: `GET /api/recordings/sessions/{id}/summary-slices` (which the recorder polls throughout the meeting) now refreshes `updated_at` as a heartbeat when `status='recording'`, and `SESSION_WATCHDOG_MINUTES` is widened 30→120 as a backstop. Genuinely abandoned/zombie sessions (no heartbeat) are still reaped.
- **Reprocess failed at transcription — worker missing all model-service env** [hotfix on v3.26.15] — the `meet-bulk-import-worker` (which runs the Arq reprocess pipeline) was deployed without any of the STT/diarize/speaker/embedding/LLM/Qdrant endpoints the backend has, so transcription failed with `Parakeet client error: Temporary failure in name resolution` → `Transcription produced no segments`. Mirrored the 24 model-service vars (`PARAKEET_SERVER_URL`, `SORTFORMER_URL`, `SPEAKER_SVC_URL`, `INFINITY_EMBEDDING_ENDPOINT`, `MEETING_OPS_LLM_URL`, `OPENAI_API_BASE`, `QDRANT_URL`, `GARAGE_ATTACHMENTS_BUCKET`, etc.) onto the worker in the compose. Fourth and final defect in the broken-since-June-5 Arq reprocess path — net effect: **every server-side reprocess (the canonical transcript/diarization/summary/title) had silently failed since June 5; recordings finalized but never produced a summary**, getting watchdog-failed ~6h later. (Follow-up: the worker/backend env should be DRY'd via a YAML anchor so they can't drift again.)
- **Reprocess crashed with `NoReferencedTableError: ... conference_rooms`** [hotfix on v3.26.15] — the arq worker imports a narrower module set than the FastAPI app, so `database/models_rooms.py` (which defines the `conference_rooms` table / `ConferenceRoom` model) wasn't registered in the worker's SQLAlchemy metadata; every `RecordingSession` query in the reprocess pipeline then failed to resolve the `room_id` FK. Registered `models_rooms` at the worker entrypoint (`workers/bulk_import_worker.py`) so the FK resolves for all jobs (the prior per-helper try-import only covered one function and silently swallowed failures). Third defect in the broken-since-June-5 Arq reprocess path, after the DB-mismatch and `_job_id` fixes.
- **Reprocess jobs crashed instantly with `TypeError: ... unexpected keyword argument 'job_id'`** [hotfix on v3.26.15] — `enqueue_reprocess` (`workers/reprocess_workers.py`) passed `job_id=` to `pool.enqueue_job`, but Arq's reserved kwargs are underscore-prefixed (`_job_id`); the bare `job_id` was forwarded into `reprocess_session_job()` (which has no such param), so every reprocess job failed on pickup. Changed to `_job_id` (which also serves as the per-session idempotency key). Latent behind the DB-mismatch bug below — jobs never reached a worker to hit it until that was fixed.
- **Always-on recordings never finished processing (reprocess queue DB mismatch)** [hotfix on v3.26.15] — the Arq reprocess worker (`meet-bulk-import-worker`) consumes from `ARQ_REDIS_URL` (Redis **db 4**), but the backend service had no `ARQ_REDIS_URL`, so `get_arq_pool()` (`workers/bulk_import_worker.py`) fell back to `REDIS_URL` (**db 6**) and enqueued every `finalize_session_job` to a DB no worker watches. Recordings uploaded + finalized fine but sat at `status=processing` until the session watchdog failed them ~6h later — no canonical transcript/summary/title ever produced. Added `ARQ_REDIS_URL=...redis/4` to the backend compose env so enqueue and consume share db 4. Backend env-only change; the on-startup reprocess-recovery re-enqueues any in-flight `processing` sessions onto the correct queue. Latent since the v3.26 (P-00065) Arq reprocess queue shipped — the worker got the env var, the backend didn't.

### Added
- **Knowledge Graph page (person-centric, ship-dark)** [v3.26.15] — a new `/knowledge-graph` page surfaces how a person connects across your meetings (their meetings, co-speakers, topics, decisions, action items) by rendering their Brigade (FalkorDB) subgraph in the existing 3D viewer. New backend `GET /api/knowledge-graph/person/{speaker_id}?hops=1|2` resolves the speaker ORG-SCOPED first (cross-org → 404, no existence leak), pulls a depth-1/2 neighborhood via `fetch_entity_context`, filters returned nodes by `org_id` in shared-tenancy mode (structural isolation in `per_org_graph`), caps at `KNOWLEDGE_GRAPH_NODE_CAP` (default 250), and strips routing/tenancy props on the wire; tier-gated to `brigade_integration` (Pro+). Frontend hidden behind `VITE_KNOWLEDGE_GRAPH_PAGE_ENABLED` (default OFF). `BrigadeGraphViewer` gained an optional `data` prop so the page owns the fetch + person-appropriate empty states (not_synced_yet / single_meeting / live_failed / truncated); its per-session usage is unchanged. No DB migration.
- **Fixed: `full-audio 404: Session not found` on Stop** [v3.26.14] — the desktop always-on recorder created the session via `start-always-on` with NO `X-MeetingOps-Org` header (so the row was filed under the user's *first* org) but uploaded to `/full-audio` with the *active*-org header. For multi-org users whose active org wasn't their first, the stop-time upload 404'd, so the canonical server reprocess (high-quality transcript + diarization + summary + title) never ran and the session could hang at `status='recording'` until the watchdog. The recorder now sends the org header on `start-always-on`, `/finalize`, and `/finalize-audio`, and **pins the creating org's slug at create time** (`sessionOrgSlugRef`), reusing it for `/full-audio` + `/finalize` at Stop so every call resolves to the same org even after a mid-session org switch — matching the mobile recorder, which was already correct. Audio was never lost (it stays buffered in IndexedDB until a successful upload).
- **Fixed: Pause then couldn't resume a recording** [v3.26.14] — on Pause, the `@ricky0123/vad-web` library's default `pauseStream` called `track.stop()` on the mic (permanent), and its default `resumeStream` re-opened a fresh *default* mic that ignored the user's selected source — leaving the parallel full-audio `MediaRecorder` bound to a dead stream (silent after resume). `vadEngine` now passes a no-op `pauseStream` + identity `resumeStream`, so the user's selected capture stream stays live across pause/resume (MicVAD only disconnects/reconnects its internal node graph, which is all that's needed to gate frame processing).
- **Fixed: Transcription showed the server GPU while running in-browser** [v3.26.14] — the Agent & Pipeline status panel hard-wired the Transcription sublabel to the server GPU name (e.g. "RTX 3060") even when transcription was running on-device. The Transcription and Summarizer rows now show **"On-device (browser)"** whenever the active stage is browser-tier, and the server GPU/route only when the stage actually runs server-side.
- **Audio-source cards slimmed + hover tooltip** [v3.26.14] — the "Just me" / "Me + system audio" cards drop the long inline blurb (now a hover tooltip) and use tighter padding, so the record screen is less cluttered; the Advanced manual source picker below is unchanged.
- **Resizable live panels (desktop)** [v3.26.14] — the Live Transcript and Live Summary panels now have a native vertical resize handle (drag to make them taller/shorter) with a sensible min-height; ignored on touch.
- **Fixed: always-on recordings stuck at "processing" forever** [v3.26.13] — the full-audio reprocess pipeline (`_run_session_reprocess` in `api/recording.py`) tracked its own progress in `processing_metadata.reprocess_status` but NEVER flipped the top-level `session.status`. So every always-on recording sat at `status='processing'` in the Sessions list even after a clean `reprocess_status='complete'` (full transcript + summary + title all done) — a permanent spinner the 30-min/6-hour watchdog then had to mass-resolve hours later (and which produced the 76-session stuck pile we just cleaned up). `_set_status` now maps the terminal reprocess states onto `session.status` (complete→completed, failed→failed, sets `ended_at`); intermediate states (queued, in_progress) stay 'processing'. Backend change — backend image rebuilt + redeployed.
- **Fixed: "always-on already on" stuck flag** [v3.26.12] — the cross-surface recording-presence flag (`meetingops.alwayson.active`) was a sticky boolean with no expiry (the `isStaleFlag()` heuristic its own comment promised was never built). A tab killed mid-recording (common on the pre-v3.26.10 mobile in-memory path) stranded it at 'true', so every later Start was blocked with "always-on already on" — and since the SW self-heal clears caches but NOT localStorage, it survived onto the new app. Flags now store a heartbeat timestamp the live surface refreshes every ~1s; `readFlag` treats a flag whose heartbeat is >90s stale as dead and self-heals it, and clears legacy 'true' values on sight. A real session never trips it; a crashed tab's flag clears on the next read.
- **Zombie service-worker self-heal** [v3.26.11] — devices that loaded a PWA-enabled build (v3.20.x-v3.22.1) still had a workbox service worker registered, serving a stale PRECACHED app shell from their own cache and bypassing the server entirely — so record-first + all the v3.26.x mobile work never reached them (they saw the old dashboard-first app + "always-on already on", even on the right URL with caches cold server-side). The v3.22.2 kill-switch 404'd `/sw.js`, but a 404 does NOT unregister an already-registered SW, and the self-destruct worker was mistakenly served at `/sw-kill.js` — a URL no zombie ever polls. Fix: serve the self-destruct worker at `/sw.js` (the script URL zombies actually update-check). On the next navigation the browser installs it; on activate it unregisters itself, clears EVERY cache, and reloads, after which the device fetches the current app from origin. No fetch handler, so it cannot re-enter the v3.22.2 fetch loop; runs exactly once per device (the app registers no SW, so nothing re-registers). Inert for devices that never had a zombie.
- **Live transcript + live summary restored on capable phones; live summary fixed everywhere** [v3.26.10] — (1) Phones/tablets with WebGPU are now a new `mobile-capable` device class that runs the on-device Parakeet STT + summary LLM and buffers to IndexedDB, exactly like desktop — so the live transcript AND live summary come back on the phone (older non-WebGPU phones stay capture-only). (2) Fixed the live summary that silently did nothing in standard mode on EVERY device: v3.26.9 stopped streaming transcript to the server during recording, but standard-mode summary still POSTed `/summary-slices` to a server with no transcript (→ 422 — the x/500 meter advanced but no summary appeared). The live summary now always runs on the on-device LLM (standard + privacy); the server still produces the CANONICAL summary from the whole-file reprocess at Stop. (3) Mobile recordings are now durable (IndexedDB, recoverable after a tab suspend/kill) instead of volatile in-memory.
- **Always-on recorder reworked to local-then-upload** [v3.26.9] — during a meeting NOTHING streams to the server (the live transcript comes from in-browser STT); on Stop the whole recorded audio is uploaded ONCE via `/full-audio` and the server runs the final transcription, diarization, summary, and title. Removes the per-chunk streaming + the verify/reconcile dance, so a clean Stop no longer shows the "unprocessed chunks → discard/reupload" prompt. Mobile uses an in-memory buffer; device hot-swap re-attaches the recorder; manual "Start new session" still splits. The summary is now end-of-meeting (the live in-meeting summary is intentionally gone in standard mode). Backend `_safe_ext` now accepts `.mp4` for Safari/iOS blobs.
- **Fixed: always-on recorder split one meeting into many sessions** [v3.26.8] — the v3.23.2 "disable silence-split" hotfix set `silenceThresholdMs: Number.MAX_SAFE_INTEGER`, but `setTimeout` truncates its delay to a signed 32-bit int (`MAX_SAFE_INTEGER & 0xFFFFFFFF === -1` → clamped to 0ms), so the silence timer fired on the next tick after EVERY utterance → `splitSession()` → a brand-new session per utterance. Unwired `onSilenceThreshold` from the VAD engine (now a no-op) + replaced the footgun value. A meeting is now ONE continuous session — which also unblocks the live summary, since the transcript now accumulates past the 500-word slice trigger. Manual "Start new session" still splits.
- **Fixed: superuser-gated admin routes returned 500 instead of a clean 403** [v3.26.7] — `simple_settings`, `agent_management`, and `live_transcription` had a bare `except Exception` that re-wrapped the inner `HTTPException(403)` as a 500. Added `except HTTPException: raise` guards so non-superusers get a proper 403 (access was always denied; the error was just ungraceful). Fixes the v3.18.1-audit `test_unauth_admin_routers_require_auth`.
- **Bulk export** [v3.26.6] — "Export selected" on the Sessions multi-select downloads a zip of per-session markdown (title, date, status, summary, transcript, action items) via `POST /api/simple/recording-sessions/bulk-export` (org-scoped). Nav "Bulk Import" renamed to "Import / Export" to keep the menu uncluttered.
- **Clearer summary-format toggles** [v3.26.6] — on the record screen, selected formats now show a fuchsia→indigo gradient with a check, unselected are muted zinc, so the selection state reads at a glance.
- **Sessions multi-select + bulk delete** [v3.26.5] — the Sessions list now has per-row checkboxes, select-all, and a bulk-action bar to delete many sessions at once (`POST /api/simple/recording-sessions/bulk-delete`, org-scoped, same deletion path as single-delete).
- **Auto-title for short sessions** [v3.26.5] — finalize now generates a real title from the transcript even when a session is too short to summarize, fixing always-on fragments stuck at "Always-on {date}"; plus a `backfill_session_titles.py` script (dry-run default, `--commit`) to retitle the ~77 existing ones.
- **Session Details header scrolls off** [v3.26.4] — the saved-session header + tab sub-bar no longer stay pinned (removed `sticky`); they scroll away with the page so the session content leads.
- **Compact Session Details header** [v3.26.3] — the sticky saved-session header (title + action buttons) was too tall and stayed pinned; reduced its vertical padding + title size and re-docked the dependent sub-bar offset (top-[97px] -> top-[80px]) so the session view leads with content.
- **Compact record-screen header** [v3.26.2] — shrank the oversized "Live Recording" page header (smaller title + icon, dropped the marketing subtitle, tighter margin) so the record screen leads with recording, not chrome.
- **Record-screen fixes** [v3.26.1] — fixed live **summary + transcript auto-scroll** (the real AlwaysOnControl panels now stick-to-bottom; the prior pass had wired scroll to duplicate panels). Removed the **duplicate live panels + the second "Advanced"** from the record page so AlwaysOnControl is the single live surface. Moved the **live transcript toward the top**. Added a **collapsible + showable desktop sidebar** (icons-only rail, persists across reloads; mobile drawer unchanged).
- **Fixed: false error after ending an always-on recording** [v3.26.1] — stopping a recording showed a spurious error + "unfinished recording" banner even though the meeting finalized and processed (backend returned 202/200). Root cause: the stop fallback path never set the local IndexedDB `finalized` flag, so the orphan rescan re-flagged the just-saved session. A clean stop now clears stale errors, marks the session finalized before the rescan, and shows "Saved — processing your meeting." Genuine mid-upload-crash recovery is preserved.
- **Live recording screen polish** [v3.26.0] — the live transcript and live summary now **auto-scroll** (stick-to-bottom; pauses when you scroll up, resumes at the bottom). Surfaced two streams that were captured but never rendered as real **Live Transcript** + **Live Summary** panels. Added a focus-metrics row (elapsed, words-to-next-summary, speaker count, a VAD/listening pulse) and a **speaker-count** chip on the Pro server-live (Sortformer) transcript. Decluttered: summary-format toggles and progressive/debug internals tuck behind disclosures.
- **Project-Ops auto-push is now opt-in** [v3.26.0] — extracted action items no longer auto-push to Project-Ops by default. Gated behind a per-org `auto_push_action_items` flag (toggle via `PUT /api/integrations/project_ops`) with a deploy-level `PROJECTOPS_AUTO_PUSH_ACTION_ITEMS` env default (both default **off**). Extraction, storage, and in-app display are unchanged; the writer returns `mode="gated-off"` when opted out.
- **Record-first UX** [v3.25.0] — authenticated users now land on the recorder (`/record`) instead of the dashboard, and the **Record** action is promoted to a prominent top-of-nav button. Getting into a recording is the default, zero-detour path; the dashboard stays one click away.
- **Weekly email digest (automated)** [v3.24.4] — a weekly Arq cron generates a per-org digest and emails it via the existing sender; idempotent via new `meeting_digest.emailed_at` column (migration `040`). Empty windows are skipped.
- **Per-user inline speaker assignment** [v3.24.4] — non-admins can create + link a new speaker on their own meeting (`POST /api/sessions/{session_id}/speaker-links/{link_id}/create-speaker` and `/enroll`), scoped to their org + session; admins keep speaker-library management.
- **Fixed: always-on recorder empty-session pile-up** [v3.24.4] — the session watchdog now hard-deletes abandoned always-on sessions with no server audio AND no transcript, instead of marking them `failed`; real meetings are protected by the transcript guard.
- **PostHog product analytics**. Wired into App mount via `utils/posthog.ts`. Identifies user on login (id, email, tier, is_founding_member). Instruments key events: signup, recording start/stop, summary slice generated, subscription started/canceled, MCP PAT created, pricing viewed, landing invite requested, support submitted. Respects DNT, respects localStorage opt-out flag, no session replay (off by default; flip in PostHog dashboard if wanted), identified_only person profiles. New "Opt out of product analytics" checkbox in Settings → Privacy. Privacy Policy updated to disclose PostHog as a subprocessor.
- Build env vars: `VITE_POSTHOG_KEY`, `VITE_POSTHOG_HOST`.
- **Project-Ops bridge: client-credentials auto-refresh.** `ProjectOpsClient` now mints fresh service-account JWTs on demand when `PROJECTOPS_KC_TOKEN_URL` + `PROJECTOPS_KC_CLIENT_ID` + `PROJECTOPS_KC_CLIENT_SECRET` are set. Cached until ~30s before the JWT's own `exp` claim, with an `asyncio.Lock` so concurrent requests after expiry mint exactly once. Static `PROJECTOPS_API_KEY` still supported as the legacy / smoke-test path; auto-refresh wins when both are set. New `client.auth_mode` ∈ `auto-refresh|static|unconfigured` for diagnostics. 8 new tests in `tests/test_projectops_client_token.py` covering mint caching, expiry-driven re-mint, concurrent mint serialization, and 401 failure handling.
- **Meeting org reassignment**. New endpoint `PUT /api/simple/recording-sessions/{id}/organization` lets the user move a meeting from one of their orgs to another. Cascades to related rows (`action_items`, `session_attachments`, etc.). Frontend: new "Move to organization" button in SessionDetails header, org badge per row in Sessions list, org filter pills above the list. Sessions list endpoint accepts `?include_all_my_orgs=true` to show meetings from every org the user is a member of (so filter pills work without re-switching active org).

## [3.23.3] - 2026-06-02

### Fixed
- **Session watchdog wasn't committing — silent zombie pile-up**. The Arq `session_watchdog_task` cron was firing every 15 min and correctly identifying stuck recording sessions (logged `session_watchdog.force_fail session_id=...` for each candidate), but the commit at `session_watchdog.py:187` was raising `sqlalchemy.exc.NoReferencedTableError: Foreign key associated with column 'recording_sessions.organization_id' could not find table 'organizations'` and rolling back the entire pass. Cause: `mark_abandoned_recording_sessions()` imported `RecordingSession` but not `Organization`, so the worker process's SQLAlchemy metadata didn't have the `organizations` table registered when the FK resolution needed it at flush time. Added the import. Watchdog now commits cleanly. Found after bigboy accumulated 110 zombie sessions over the previous hours.
- **Manual cleanup applied** to bigboy: 110 stuck rows (sessions in `recording`/`starting`/`paused`/`stopping` > 60 min idle + sessions in `processing` > 6 h idle) marked `failed` with `[stuck-cleanup v3.23.2]` name suffix. Real session 279 (Aaron's 31-min meeting from 16:16) untouched. VPS had 0 stuck sessions — clean.

## [3.23.2] - 2026-06-02

### Fixed
- **In-recording silence-split disabled entirely**. v3.23.1 bumped the threshold from 30s → 30 min, but the right architecture is to NEVER auto-split during capture. The audio file should stay whole; splitting is a post-processing decision made AFTER the recording, in the session detail view, user-initiated by default. v3.23.2 sets `silenceThresholdMs: Number.MAX_SAFE_INTEGER` so the VAD engine's `splitSession` handler physically cannot fire mid-recording. Recording is always one session until the user presses Stop.

### Roadmap filed
- `docs/roadmap/post-process-split.md` — the full post-process "Split into meetings" feature design. Adds backend endpoints (`/proposed-splits` + `/split` + `/unsplit`), a SessionDetails timeline-scrubber modal UI for adjusting split points, and Settings → Audio toggles. Estimated 6-8 hours focused session. Scheduled for v3.25/v3.26 (after per-seat + before Brigade federation deploy).

## [3.23.1] - 2026-06-02

### Fixed
- **Silence-split was fragmenting single recordings into multiple session rows**. The AlwaysOn VAD engine's `silenceThresholdMs` was set to 30 seconds — a totally normal thinking pause in a real meeting. Aaron's 31-minute recording on bigboy got split into 6 separate `recording_sessions` rows (one real + 5 silence-gap orphans). Bumped the threshold to 30 minutes so normal meeting pauses never trigger; the always-on "capture all day, auto-split into separate meetings" use case still works because real meetings are typically separated by 30+ minutes (lunch, end-of-day, etc.). Future work: surface this as a user setting.
- Cleaned up orphan sessions 280-284 on bigboy (marked `failed` with `[silence-split orphan, v3.23.1 cleanup]` name suffix). Real session 279 untouched.

## [3.23.0] - 2026-06-02

**Strategic repricing + Basic tier shipped. Pro repriced from $12 to $20 to match the meeting-intelligence-platform positioning. New Basic $7.99 captures the mid-market wedge with text-only sync. New Suite $35 bundles Meeting-Ops Pro with sibling UC apps (cross-app entitlement lights up when Brigade federation deploys). All existing $12 subscribers grandfather automatically via Stripe.**

### Added
- **Basic tier** ($7.99/mo, $79/yr) — server-side text storage + sync + cross-meeting search + AI Chat over corpus, NO audio upload, NO server completion pass. Architectural rule: Basic gets server text processing, Pro gets server audio processing. Stripe products + prices live (`prod_Ud0M1gmwuEKJjv`, `price_1TdkLWDzk9HqAZnHPMNKYsnC` monthly + `price_1TdkLWDzk9HqAZnHNpgniogU` annual).
- **Pro tier reprice to $20/mo $200/yr** — new Stripe prices `price_1TdkLVDzk9HqAZnHQyo3D6wG` + `price_1TdkLVDzk9HqAZnHab2lHN2m`. Old $12 and $120 prices kept alive on Stripe for any existing subscribers who grandfather (their existing Stripe subscriptions tied to the legacy price IDs continue at those prices indefinitely until cancelled). `services/stripe_client.price_id_to_tier` recognizes both new + legacy IDs so the webhook resolves either to `pro` tier.
- **Suite tier** ($35/mo, $350/yr) — Meeting-Ops Pro features PLUS `uc_suite_entitlement` flag for Pro-tier benefits on sibling UC apps (Project-Ops, Contact-Ops). Today the cross-app entitlement is documentation-only; lights up when Brigade federation deploys. Suite checkouts work end-to-end; customers pay for the bundle and receive the Meeting-Ops Pro benefit immediately + the cross-app benefit when federation lands. Stripe products + prices live (`prod_Ud0Mf9oqRAN7Qq`, `price_1TdkLXDzk9HqAZnH3Wqb0srE` monthly + `price_1TdkLXDzk9HqAZnHdJmdpiO6` annual).
- **Pricing page rebuild for 5-tier matrix** (`frontend/src/pages/Pricing.tsx`). Now shows Free / Basic / Pro / Suite / Enterprise. Single Monthly/Annual toggle at top of grid; annual shows "save 2 months" on every paid tier. Pro is the highlighted "Most popular" card. Suite card has an honest one-liner about cross-app sync rolling out as federation deploys — doesn't block the sale.
- **Repositioning copy**: Landing + Pricing subheads now read "meeting and conversation intelligence platform" instead of "AI meeting transcription." Subtle but the category claim matters — competing with Gong/Chorus when bundled, not Otter/Fireflies.
- **`useTierFeatures` extended** with `audio_upload`, `server_text_storage`, `ai_chat_over_corpus`, `cross_meeting_search`, `uc_suite_entitlement`, `byok_models`. Existing tiers backfilled with consistent values.
- **Audio upload UI gate**: Basic tier sees no "Upload an existing audio file" button on Sessions page (they can still browser-record). Pro+ unchanged.
- New checkout helpers: `tier_to_price_id` resolves `basic` / `pro` / `suite` / `enterprise` × `monthly` / `annual`; `price_id_is_basic`, `price_id_is_pro`, `price_id_is_suite` reverse-lookups for the webhook. `/api/billing/checkout` accepts `plan` in `basic | pro | suite | enterprise` and returns 400 with a "contact sales" message for enterprise.
- New `tests/test_basic_tier.py` covering the architectural split (Basic 403s on `/api/uploads`, 200s on per-meeting AI chat + cross-meeting semantic search). `tests/test_stripe_webhook.py` extended with basic/suite price-flip + legacy $12 grandfather + Founding-100-not-granted-on-Suite-annual coverage.

### Changed
- **`/api/uploads/start`** (action=`transcribe`) now gates on `audio_upload` instead of `canonical_reprocess`. Both flags are False for Free + Basic so the 403 behaviour for those tiers is unchanged; the new flag is the architecturally-correct name for "server audio ingest" and decouples it from "server completion pass".
- **`/api/ai-chat/sessions/{id}/messages`** (per-meeting AI chat) now gates on `ai_chat_over_corpus` (Basic + higher) instead of `canonical_reprocess` (Pro + higher). Basic tier is the new "text-corpus" SKU and this is the line that opens AI Chat to it.
- **`/api/ai-chat/rag/query`** (cross-meeting RAG chat) now gates on `cross_meeting_search` (Basic + higher) instead of `canonical_reprocess`.
- **`/api/simple/recording-sessions/semantic-search`** now gates on `cross_meeting_search` (was un-gated for any logged-in user, which leaked the corpus surface to Free users — moat-tightening fix found during the tier work).
- **`_TIER_RANK`** widened from 3 levels (free=0, pro=1, enterprise=2) to 5 (free=0, basic=1, pro=2, suite=3, enterprise=4). Call sites using `>=` comparisons keep working.
- **Founding 100 grant** explicitly Pro-only. The webhook still fires on annual-upfront detection, but now requires the resolved tier to be `pro` before calling `_maybe_grant_founding_member`. Suite annual checkouts ($350/yr) do NOT consume a Founding 100 seat per Aaron's call 2026-06-02 — a separate Suite cohort can be opened later if it ever makes sense.

### Notes
- Per-seat pricing is NOT in this release. Each subscription = 1 user. Multi-seat support (org invites + Stripe quantity-update flow) is a separate sprint. Pro/Suite headline pricing is the per-user price; team buyers will pay per seat when that ships.
- Existing $12 Pro subscribers (none yet at v3.23.0 cut, but any pre-2026-06-02 subscriber) automatically grandfather: their Stripe subscription is tied to the legacy price ID and continues at $12/$120 indefinitely until cancelled. `price_id_to_tier` keeps both legacy Pro IDs (`price_1TcbE0Dzk9HqAZnHO3cv6mWN` $12/mo, `price_1TdcTfDzk9HqAZnHl979Vv0M` $120/yr) and the legacy Suite ID (`price_1TcbE1Dzk9HqAZnH9HVigcpm` $25/mo) mapped to their respective tiers.
- Suite cross-app entitlement (`uc_suite_entitlement` flag → Pro on Project-Ops + Contact-Ops) is documentation-only until Brigade federation deploys. Suite checkouts work end-to-end today; the cross-app benefit lights up when federation lands.
- Deploy env required on both nodes:
  - `STRIPE_PRO_PRICE_ID=price_1TdkLVDzk9HqAZnHQyo3D6wG` (was $12 price, now $20)
  - `STRIPE_PRO_ANNUAL_PRICE_ID=price_1TdkLVDzk9HqAZnHab2lHN2m` (was $120, now $200)
  - `STRIPE_BASIC_PRICE_ID=price_1TdkLWDzk9HqAZnHPMNKYsnC` (NEW)
  - `STRIPE_BASIC_ANNUAL_PRICE_ID=price_1TdkLWDzk9HqAZnHNpgniogU` (NEW)
  - `STRIPE_SUITE_PRICE_ID=price_1TdkLXDzk9HqAZnH3Wqb0srE` (was $25 archived price, now $35)
  - `STRIPE_SUITE_ANNUAL_PRICE_ID=price_1TdkLXDzk9HqAZnHdJmdpiO6` (NEW)

## [3.22.5] - 2026-06-01

### Fixed
- **Stuck-state recovery for AlwaysOn recording** (`contexts/AlwaysOnContext.tsx` + `components/AlwaysOnControl.tsx` + watchdog). When the page mounts with localStorage claiming an active recording but the server says the session is `failed`/`completed`/`stopped` (or doesn't exist), the client now wipes the local claim instead of blocking Record. A "Reset recording state" escape-hatch button surfaces when the client's recording state is `recording` but no MediaRecorder is actually capturing. Backend Arq watchdog now fails stale recording sessions at 30 minutes idle instead of 6 hours — catches phone-tab-eviction much faster.

### Added
- **Annual Pro pricing v1** — Pro tier offered an annual billing cycle at **$120/year** ($10/mo billed annually, save $24). Monthly / Annual toggle on the Pro card. `STRIPE_PRO_ANNUAL_PRICE_ID=price_1TdcTfDzk9HqAZnHl979Vv0M`. Superseded by v3.23.0's $200/yr reprice; this price ID stays alive for grandfathering.
- **Legal pages**: Terms of Service (`/terms`), Privacy Policy (`/privacy`), Acceptable Use Policy (`/aup`). Tailored for Magic Unicorn Unconventional Technology & Stuff Inc. (SC C-Corp): mandatory arbitration under SC law, liability cap at 12 months of fees ($100 for Free users), refund policy (7-day monthly / 30-day prorated annual), GDPR + CCPA compliance, HIPAA only on Enterprise + signed BAA. Recording Consent Disclosure in AUP makes the user solely responsible for participant consent under their jurisdiction's laws (critical for two-party-consent states).

### Changed
- **Founding 100 page reframed to "Coming soon"** (`frontend/src/pages/Founding100.tsx`). Holds off promoting membership until a package worthy of it lands. Page leads with "Founding 100 — Coming soon", swaps CTA for a "Notify me" waitlist form posting to `/api/landing/invite-request`. Seat counter reframes "2 / 100" as "2 early supporters and counting". Backend grant gate stays armed.
- **VPS outbound email FROM promoted** to `meetings@unicorncommander.ai` (bigboy stays on `meetings@magicunicorn.dev` for enterprise-reference branding).
- **Stripe Customer Portal** config updated with `business_profile.privacy_policy_url` and `terms_of_service_url`.

### Notes
- `SESSION_WATCHDOG_MINUTES=30` env added.
- Founding 100 waitlist re-uses `InviteRequest` rows; when cohort opens, an operator can pull the waitlist before flipping `is_open`.

## [3.22.4] - 2026-05-31

### Added
- **Refresh guard while recording** (`components/RecordingIndicator.tsx`). When `RecordingContext.isRecording` is true OR `AlwaysOnContext.state` is in `starting | recording | paused | stopping`, the page registers a `beforeunload` handler that triggers the browser's native "Leave site? Changes you made may not be saved" prompt on refresh / tab close / window close. Before this, hitting refresh during an active recording would silently kill the MediaRecorder (and the live transcript + current summary slice) with no warning. Chunks uploaded so far are safe on the server, but anything in flight or unsent is gone. The handler is a no-op when nothing is being captured so we don't nag during normal browsing.

## [3.22.3] - 2026-05-31

Quality-of-life polish + smarter first-run defaults. Two narrow changes.

### Changed
- **Audio source default is now device-class aware** (`utils/audioSourceStream.ts`). On desktops with `getDisplayMedia` (Chromium-desktop) the first-run default flips from `mic` to `mic+tab` (Me + system audio), because most desktop users are on Zoom/Meet/Teams calls and want to capture the meeting plus their mic. On phones/tablets/Safari/older browsers (no tab capture support) the default stays `mic` — those users are typically in-person and tab capture isn't supported anyway. User's explicit choice is still remembered in localStorage; this only changes the very-first-time-they-open-the-app default.

### Added
- **Live-summary progress hint** (`components/AlwaysOnControl.tsx`). The "Live summary" header now shows `234 / 500 words to next` plus a thin progress bar that fills as you talk, instead of just "234 words". The data was already tracked (`transcriptWordCount` + `summary.lastWordCountAtSummary`); just surfacing it. Time-based thresholds (every 1 minute / 5 minutes) keep the existing total-word-count display since the live timer state lives in a ref we can't read from the component.

### Notes
- No backend changes, no alembic, no env, no compose. Pure frontend rebuild.
- v3.22.3 ships on top of v3.22.2's PWA-disabled state — still no service worker.

## [3.22.2] - 2026-05-30

Emergency hotfix. After v3.22.1 deploy, both bigboy + VPS users got stuck in a tight service-worker registration loop — browsers polling `GET /sw.js` 10+ times per second, pages never settling. Production was unusable.

### Root cause (provisional)
After the rapid chain of frontend rebuilds in this evening's arc (v3.20.x → v3.22.1 in a few hours) the workbox-window auto-update path interacted with `clientsClaim: true` + the `registerType: "prompt"` config in a way that pinned some clients in a "controller never stabilizes" state. PWAUpdate's `registerSW({immediate: true})` then kept re-firing, each call triggering a new `/sw.js` fetch, install attempt, activate, claim. Not yet root-caused — repro needs a clean session to study.

### Fixed (production recovery, in order)
1. Killed the loop by docker-cp'ing a self-unregistering kill-switch `/sw.js` directly into both running `meet-frontend` containers, then deleting it entirely so nginx serves `404` for `/sw.js`. Browser registrations failed cleanly; existing kill-switch SW had already cleared caches.
2. Disabled the PWA plugin in `vite.config.ts` (`disable: true`, `injectRegister: false`). New builds emit NO sw.js, NO registerSW.js, NO PWA references in `index.html`. The SPA still works fully — just no precache, no offline.
3. `PWAUpdate.tsx` left in source unchanged. Its dynamic import of `virtual:pwa-register` falls into its existing catch block when the plugin is disabled, so the component is a no-op without a code change. (Re-enabling PWA later flips one config flag.)
4. New `frontend/public/sw-kill.js` shipped as documentation + a ready-to-deploy kill-switch for future operators who hit similar SW loops.

### Side effects users will see
- "Install Meeting-Ops as an app" PWA install prompt is gone (browsers won't offer it without a manifest + SW).
- Offline page caching is gone (live network for everything; matches a normal SPA).
- The "Update available — Reload now" banner won't appear (irrelevant without SW).

### Re-enabling PWA
Flip `disable: false` + `injectRegister: 'auto'` in `vite.config.ts`. Don't do that until the loop's root cause is reproduced + fixed.

## [3.22.1] - 2026-05-30

### Fixed

- **Auth emails ported from SMTP to Postmark** (`backend/auth/email.py`). Both `send_verification_email` and `send_password_reset_email` now use the Postmark HTTP API — the same delivery path the v3.21.x landing-invite, support-contact, session-email, and session-permission flows already use. SMTP was never wired in prod, so signup verification silently no-op'd prior to this patch; password-reset emails likewise never went out (the v3.21.0 release notes incorrectly claimed Postmark for password reset — it was still on SMTP). Token resolution (`POSTMARK_API_TOKEN` → `POSTMARK_SERVER_TOKEN`), message stream, and soft-fail-on-error semantics match the existing call sites. From address resolves `POSTMARK_FROM_EMAIL` (compose env, canonical) → `POSTMARK_FROM` (older call sites) → default `no-reply@meeting-ops.unicorncommander.ai`, with optional `POSTMARK_FROM_NAME` decoration. Function signatures unchanged so call sites in `auth/routes.py` need no update. SMTP code path removed — Postmark is the only transport now. Requires `POSTMARK_API_TOKEN` (already set on both prod nodes) + a verified sender signature on the Postmark account matching `POSTMARK_FROM_EMAIL`.

## [3.22.0] - 2026-05-30

Strategic ecosystem release. The big idea: **the user's AI is the integration layer.** Two narrow changes that compound:

1. **Project-Ops action-item bridge promoted from dark to release-shipped** — the one deterministic event-time integration that earns its own code path (meeting ends → action items become PO tasks).
2. **Cross-app reference hints in Meeting-Ops MCP results** — every MCP tool result now carries optional handles to sibling UC apps so the user's AI client (Claude Desktop, Cursor, etc.) can navigate Meeting-Ops ↔ Contact-Ops ↔ Project-Ops ↔ Crisis-Ops without us coding hard integrations.

This is the "thin server-side glue + smart MCP" pattern. We don't build N×N connectors. We give the AI enough structured references to do the cross-app reasoning itself.

### Cross-app reference hints in MCP results

- New module `backend/services/mcp_cross_app.py` (pure functions, no I/O):
  - `build_cross_app_references(session)` derives `mentioned_contacts` / `mentioned_projects` / `mentioned_cases` from data the backend already returns (participants list, diarized speakers, `project_app` pointer, title + summary text scan).
  - `render_cross_app_section(refs)` emits a markdown section with a fenced JSON block carrying the structured payload for AI clients to parse.
  - Sibling URLs configurable via `CROSS_APP_CONTACT_OPS_URL` / `CROSS_APP_PROJECT_OPS_URL` / `CROSS_APP_CRISIS_OPS_URL`. Defaults: `https://contacts.magicunicorn.dev`, `https://project-ops.unicorncommander.ai`, `https://crisis.magicunicorn.dev`.
  - Confidence levels: `0.95` for structured fields (participant with email, `project_app` pointer), `0.70` for diarized-speaker names, `0.40` for keyword-only text matches. Generic `Speaker 1` / `SPK_00` labels are dropped.
- `services/mcp_app.py`:
  - `get_meeting_details` and `get_meeting_transcript` now append a `## Cross-App References` section (with embedded JSON block) when any hint was derived.
  - `search_meetings` docstring points AI clients at the new tool for per-result hint resolution (kept lightweight — no N×detail-fetches).
  - New `get_cross_app_hints(session_id)` tool returns the `cross_app_references` block standalone as JSON. Returns the canonical empty shape on backend error so AI clients can rely on the schema unconditionally.
  - `READONLY_TOOL_NAMES` bumped from 8 → 9. Both stdio (`mcp/meeting_ops_mcp.py`) and hosted-HTTP (`backend/api/mcp_http.py`) transports inherit the new tool through the shared FastMCP instance.
- Tests: new `backend/tests/test_mcp_cross_app.py` (14 cases) covers the pure-function populator, markdown rendering, the three MCP tools, and the empty-shape fallback. `tests/test_mcp_app_shared.py` count assertion updated 8 → 9.

Schema:

```
"cross_app_references": {
  "mentioned_contacts": [{"name", "email", "confidence",
                          "contact_ops_hint": {"app", "url", "query"}}],
  "mentioned_projects": [{"name", "confidence",
                          "project_ops_hint": {"app", "url", "query"}}],
  "mentioned_cases":    [{"name", "confidence",
                          "crisis_ops_hint": {"app", "url", "query"}}]
}
```

### Project-Ops action-item bridge — release-shipped The bridge code itself landed dark in v3.17.0 (2026-05-28) — best-effort one-way write from Meeting-Ops to Project-Ops on meeting completion, with `is_live` gating on `PROJECTOPS_API_KEY` and idempotent `raw_payload.po_task_id` stamping. v3.19.0 layered per-org integration toggles (`integrations.project_ops.enabled` + encrypted-at-rest credentials) and an `org_override` source. v3.22.0 ships nothing new on the code path — it consolidates the release line so the integration can be activated cleanly on prod (VPS + bigboy) by env-paste alone.

### Activation (no code deploy needed beyond bumping to v3.22.0)

The bridge is fully inert until `PROJECTOPS_API_KEY` is present. To turn it on per node:

- `PROJECTOPS_API_KEY` — Keycloak service-account JWT for an `ADMIN` or `MANAGER` identity on Project-Ops (NOT an X-API-Key; Project-Ops uses `JwtAuthGuard`). Required to flip `ProjectOpsClient.is_live` to True.
- `PROJECTOPS_BASE_URL` — optional; defaults to `http://project-ops-backend:3201/api/v1` (the in-cluster NestJS service + `/api/v1` global prefix). Override only for cross-host or external Project-Ops.

Then set a per-org target project either via:
- Org-scoped: `Organization.settings.projectops_default_project_number = "P-XXXXX"` (set via the v3.19.0 integration credentials UI, or the backfill script's `--set-default-project ORG=P-XXXXX` helper).
- Or attach a Meeting-Ops session to a PO project via the existing `session.project_app = "project-ops"` + `session.project_id = <PO project UUID>` link; the writer honors it as the most-specific resolution source.
- Or per-meeting override: `session.processing_metadata.po_project_override = "P-XXXXX"`.

### Inert-when-unconfigured guarantee

`ProjectOpsClient.is_live` returns False when `PROJECTOPS_API_KEY` is unset (`backend/services/projectops_client.py:154-158`). The writer checks `is_live` before any HTTP call and returns `ProjectOpsWriteResult(ok=True, mode="no-op")` (`backend/services/projectops_writer.py:328-334`). All callsites in `backend/api/recording.py` (reprocess Stage 7 line 1850; live finalize background task line 998) and `backend/api/action_items.py` PATCH line 333 are wrapped in top-level try/except that swallow into `logger.warning`. Result: a deployment without the env var completes every meeting cleanly with no PO HTTP traffic and no exceptions.

### Project-Ops API contract — verified

- `Authorization: Bearer <JWT>` (not `X-API-Key`).
- `POST /api/v1/tasks` body uses `projectId: <UUID>` (not number). Number → UUID resolution pages `GET /api/v1/projects?page=&limit=` client-side because Project-Ops exposes no by-number REST route. Per-client cache memoizes resolutions for the batch.
- `PATCH /api/v1/tasks/{id}/status` propagates Meeting-Ops `done`/`cancelled`/`doing` → `COMPLETED`/`CANCELLED`/`IN_PROGRESS` when the user updates a bridged action item.

### Idempotency

Each created PO task stamps `action_items.raw_payload` with `po_task_id`, `po_project_number`, `po_synced_at`. The writer treats any row already carrying `po_task_id` as bridged — never re-creates, only best-effort nudges status. Reprocess re-runs and the manual backfill script (`backend/scripts/backfill_action_items_to_projectops.py`) are both safe to run repeatedly.

### Notes

- Same risk posture as `services/brigade_writer` (best-effort, swallow-all, never blocks the meeting pipeline). Brigade and Project-Ops writes are independent fire-and-forget background tasks on the live finalize path so a PO outage cannot affect Brigade and vice versa.
- No frontend changes in this version. The integration credentials UI (Brigade/Project-Ops/Contact-Ops/Accounting-Ops/Stable) shipped in v3.19.0 already exposes the per-org PO config surface.
- No alembic migrations. The `raw_payload` JSONB column on `action_items` already exists; the org default lives inside the existing `Organization.settings` JSONB column.

### Brigade federation deferred

Survey this release found Brigade is NOT a federation broker today — has agent orchestration + A2A protocol surface + MCP tool catalog for its own agents + external MCP registry, but every piece of the federation broker pattern (JWKS, JWT mint, streamable-HTTP /mcp transport, /.well-known/mcps.json discovery, manifest loader, namespaced catalog, proxy forwarder, scope enforcement) is unbuilt. Estimated 9 engineering days for Phase 1 (skipping OIDC) or 3 days for a smaller "JWT mint + /mcp mount + ONE hardcoded tool" proof. Design doc at `~/Documents/brigade-federation-design.md`. Defer to a focused multi-day session.

## [3.21.2] - 2026-05-30

Second hotfix in the v3.21.x series. Bigboy (dev) had the legacy `ix_users_is_founder` index from an earlier dev migration; VPS (prod) did not — so VPS hit the next trap: in Postgres, `op.drop_index` wrapped in a Python `try/except` does not actually "ignore" a missing-index error. Postgres aborts the entire transaction on the failed DDL, the Python `except` catches the exception but the transaction stays in `InFailedSqlTransaction` state, and the very next DDL (`ALTER TABLE users RENAME is_founder TO is_founding_member`) fails with "current transaction is aborted, commands ignored until end of transaction block." Backend crash-looped on VPS.

### Fixed
- `037a_founding_pwreset.py`: replace `try: op.drop_index(...) except: pass` with idempotent SQL `DROP INDEX IF EXISTS ix_users_is_founder`. Postgres short-circuits cleanly when the index is missing, keeping the surrounding transaction healthy. SQLite supports the same syntax — no dialect split needed.

This is the right portable fix. The earlier try/except pattern looked safe but was a Postgres footgun masquerading as defensive code.

## [3.21.1] - 2026-05-30

Hotfix for v3.21.0: the auth-features migration `037_founding_member_and_password_reset` had a 38-char revision identifier, but Postgres `alembic_version.version_num` is `VARCHAR(32)`. Upgrades to v3.21.0 silently rolled back at COMMIT time (DDL applied inside the transaction, then the version-table INSERT exploded with `StringDataRightTruncation` and Postgres rolled the whole upgrade). Backend crash-looped on bigboy first-deploy attempt.

### Fixed
- Renamed alembic revision `037_founding_member_and_password_reset` → `037a_founding_pwreset` (21 chars). Filename unchanged for descriptiveness. Updated the merge migration's `down_revision` tuple to reference the new identifier.

### Added
- `039_widen_alembic_ver`: defensive migration that widens `alembic_version.version_num` from `VARCHAR(32)` to `VARCHAR(255)` (alembic's modern default for new projects). Prevents this entire failure mode for future migrations. Safe to run repeatedly — Postgres ALTER TYPE on VARCHAR-to-wider-VARCHAR is metadata-only.

## [3.21.0] - 2026-05-30

Multi-feature release on top of v3.20.1 — Founding 100 cohort mechanic (rename + atomic cap), password reset (self-serve), landing CTA full-path (form → /signup), and customer support contact form. Four parallel-agent streams merged via no-op alembic merge migration. Three follow-on Aaron-led items are still parked (Stripe live env paste on VPS, Postmark token on VPS, `ALLOW_REGISTRATION` flip) — those involve live secrets/public-flip and stay manual.

### Founding 100 cohort mechanic (renamed from "Founders 100")

- **Founding 100 cohort mechanic (renamed from "Founders 100")**: explicit cohort label + atomic grant-on-Stripe-annual-upfront.
  - `users.is_founder` renamed to `users.is_founding_member`; new `users.founding_cohort` (varchar(64), nullable). Old `is_founder` column FULLY removed (alembic 037 + every callsite swept).
  - Grant trigger moved from signup time to Stripe annual-upfront completion (`customer.subscription.created` with `recurring.interval == "year"` OR `checkout.session.completed` with `metadata.annual_upfront == "true"`). Monthly subscriptions never consume a seat.
  - `api/stripe_webhook.py`: new `_maybe_grant_founding_member(db, user, cohort=...)` with PostgreSQL `SELECT ... FOR UPDATE` cohort-row lock so two concurrent webhooks can't both award seat 100. Logs structured event `founding_member_granted user_id=N cohort=meeting_ops_v1 seat_num=N capacity=100`. `_maybe_grant_founder` kept as a thin backward-compat alias through v3.21.x.
  - Env rename: `FOUNDING_100_ACTIVE` / `FOUNDING_100_LIMIT` are now canonical. Legacy `FOUNDERS_100_*` names still read as a fallback so a stale env file boots cleanly through the transition.
  - New `api/founding.py` (router + admin_router):
    - `GET /api/founding/status?cohort=meeting_ops_v1` — public, 60s in-process cached, returns `{cohort, seats_taken, seats_total, is_open}`.
    - `POST /api/admin/founding/close` (admin-only) — sticky kill-switch (process-local + Redis-mirrored) that stops future grants for the cohort even when nominally below cap. `FOUNDING_COHORT_CLOSED=true` env is the restart-safe escalation.
  - `auth/tier.py`: `_FOUNDING_MEMBER_OVERLAY` now keys off `is_founding_member` (was `is_founder`).
  - `auth/routes.py.UserResponse`: `is_founder` field removed; `is_founding_member: bool` + `founding_cohort: Optional[str]` added.
  - `api/billing.py.SubscriptionResponse`: same rename; consumers of `/api/billing/subscription` must read `is_founding_member`.

- **Password reset**: native email/password reset for the VPS auth path (no oauth2-proxy).
  - New `password_reset_tokens` table (alembic 037): `id`, `user_id`, `token_hash` (sha256 of the 32-byte base64url plaintext), `expires_at`, `used_at`, `created_at`. Single-use, 1-hour TTL.
  - `POST /api/auth/password/forgot` — body `{"email": "..."}`. Always returns 202 with the same body regardless of whether the email matches a user (never leak account existence). Rate-limited 3/hour/email via Redis SETEX (in-proc fallback when Redis unavailable). Sends the reset link via `send_password_reset_email()` (Postmark/SMTP, soft-fail).
  - `GET /api/auth/password/reset/validate?token=...` — lightweight 200/400/410 token state check so the form can render before the user types.
  - `POST /api/auth/password/reset` — body `{"token": "...", "new_password": "..."}`. 200 on success, 400 on weak/unknown, 410 on expired/used. Marks `used_at` so the same token can't double-reset.

- **Founding 100 frontend page**: new public route `/founding-100` (`pages/Founding100.tsx`) — live seat counter via `GET /api/founding/status`, progress bar, six-perk grid (lifetime price-lock, private Discord, advisory council seat, quarterly roadmap vote, annual founders summit, cross-app early access), CTA to `/pricing` when `is_open`, "Cohort closed — thank you" when full.
- **Password reset frontend**: new `/forgot-password` (`pages/ForgotPassword.tsx`) and `/reset-password?token=...` (`pages/ResetPassword.tsx`). Reset page calls the validate endpoint on mount and renders an "expired link" CTA on 400/410; on success bounces to `/login?password_reset=success`.
- **Login**: new "Forgot your password?" link under the Sign-in button.
- Frontend types swept: `services/billingApi.ts.SubscriptionState.is_founder` → `is_founding_member`, optional `founding_cohort`; `__tests__/Sessions.test.tsx` mock user updated.
- Tests: `tests/test_founders_100.py` rewritten for the new cohort semantics + admin-close gate + public status endpoint. New `tests/test_password_reset.py` (7 scenarios: 202 on unknown, token-issue, validate states, happy-path, weak-password rejection, rate limit). `tests/test_billing_api.py` + `tests/test_stripe_webhook.py` updated for env + response-field rename.

### Landing CTA full-path
- `pages/Landing.tsx`: invite form now fire-and-forget posts to `/api/landing/invite-request` AND redirects to `/#/signup?email=<encoded>` on submit. The email is logged either way (invite-request still writes the row), so manual approval still works for the first cohort.
- `pages/Signup.tsx`: reads `?email=` from URL on mount, pre-fills email. Detects 403 "registration closed" from `/api/auth/register` and renders a friendly "Thanks — we got your invite request" panel instead of the signup form (relogs to invite-request as belt-and-braces). CTA copy: "Request an invite" → "Get started".
- Frontend tests rewritten — 4 Landing assertions now check the full-path redirect.

### Customer support contact form
- New `pages/Contact.tsx` at `/contact`. Public (renders for authed AND unauthed). Authed callers get name+email pre-filled from `AuthContext`. Fields: name, email, subject, message.
- New `api/support.py`: `POST /api/support/contact` writes a row to new `support_requests` table (alembic 037 in the frontend-polish branch), rate-limited 3/hour/email via Redis SETEX with in-proc fallback. Fires Postmark email to `support@magicunicorn.tech` with `ReplyTo` set to submitter email so support can reply directly from the inbox.
- Footer link "Contact Support" added to `AppLayout` (every authed page), Help.tsx "Still stuck?" section, and Landing footer (unauthed visitors).

### Per-org integration credentials UI
- **Already shipped in v3.19.0** — verified by the frontend-polish agent and called out here for completeness. `backend/api/integrations_org.py` (GET masked + PUT encrypted + POST `/test` probe + DELETE clear, admin-gated) + `frontend/src/components/settings/IntegrationsPanel.tsx` (full UI for Brigade / Project-Ops / Contact-Ops / Accounting-Ops / Stable with test button, masked last-4, "Disable & clear" action). No code change needed.

### Alembic head reconciliation
- New `038_merge_v3_21_heads.py` no-op merge migration with tuple `down_revision = ("037_founding_member_and_password_reset", "037_support_requests")`. Single head restored.

### Verified
- Frontend `npm run build`: 0 TS errors. PWA precache built.
- Backend focused tests (founders + password_reset + support_contact + landing_invite + auth + health + free_tier + consumer_signup): 74/74 pass.
- Frontend vitest: 109/109 pass.
- Streaming tests still error on the pre-existing prometheus_client local-env Counter import issue. Production Docker has the right version.

## [3.20.1] - 2026-05-30

Focused tier-gate + onboarding patch on top of the v3.20.0 prod launch. Two narrow product decisions:

1. **Live Sortformer diarization is now Enterprise / Founding 100 only**, even when the instance-wide `STREAMING_USE_SORTFORMER` env is on. Pro users still get full server-live Parakeet streaming transcripts; the completion-time pyannote pass continues to give Pro full speaker-attributed transcripts at session end. The decision: unbounded per-stream GPU exposure (Sortformer runs continuously for every minute of every concurrent meeting) doesn't justify the marginal value of live speaker labels for the Pro tier. The completion pass already gives Pro everything they need.
2. **First-run onboarding now warns about the browser-model download.** Pre-v3.20.1, a new user clicked Record for the first time and ~1.4 GB of weights (Parakeet 0.6B INT8 + Qwen 3 0.6B, or larger Gemma 4 E2B on WebGPU) downloaded silently while they waited. New first-position OnboardingChecklist card explains the download, cites the real size, and offers a "Start download now" CTA that pre-loads both models in the background so the first Record click is instant. Hidden entirely on capture-only mobile (those users are on the server-completion path, no browser models).

### Backend
- `auth/tier.py`: new `live_diarization` feature flag — free=False, **pro=False**, enterprise=True. Existing `diarization` flag (completion-time pyannote) unchanged, still Pro+.
- `auth/tier.py`: new `_FOUNDING_MEMBER_OVERLAY` dict + `get_tier_features()` overlay. `is_founder=True` users inherit `live_diarization=True` regardless of base tier. Kept tight on purpose — Founding 100 is perks-only, not a free upgrade to enterprise. Only flags genuinely in the "first-look benefit" category go here.
- `api/streaming.py`: `_SessionState.live_diarization_allowed` resolved at WS upgrade from the user's effective features. The per-chunk Sortformer dispatch (`if STREAMING_USE_SORTFORMER:`) is now `if STREAMING_USE_SORTFORMER and state.live_diarization_allowed:` — Pro users open WS streams normally, just never dispatch the sortformer side-task.

### Frontend
- `components/OnboardingChecklist.tsx`: new first-position card (`Download` lucide icon, sky palette). Body explains Parakeet ~890 MB + small LLM ~570 MB = ~1.4 GB on default Qwen, more on Gemma. CTA navigates to `/record` AND fires `inBrowserSTT.load()` + `inBrowserLLM.preloadModel(getStoredModelId())` in the background. New `onClick?` field on `ChecklistItem` interface lets any card optionally trigger a side-effect alongside navigation. Header now reads "Get set up in {N} quick steps" so the count tracks the actual card count (5 desktop / 4 capture-only mobile). Grid widened to `xl:grid-cols-5` so all 5 fit on a row at xl.
- `pages/Help.tsx`: new card in the "Your computer does the real-time work" section explaining the ~1.4 GB total + breakdown, links to `docs/browser-models.md`. Footer paragraph also links to the same doc.

### Docs
- `docs/browser-models.md` (163 lines): canonical reference for what runs in the browser, per device class. Sections: what runs in your browser (Parakeet + LLM with real model sizes from source), device classes table (phones/tablets → capture-only; desktop fallback → Qwen; WebGPU desktop → Gemma 4 E2B), first-run download (~1.4 GB-3 GB depending on path, IndexedDB + CacheStorage caching, one-time), failure-mode recovery, what goes to the server vs stays local per tier, Privacy mode.

## [3.20.0] - 2026-05-30

Single overnight arc that took Meeting-Ops from "polished dev" to "production live with revenue infrastructure + multi-AI-client integration story." Bundles the entire v3.18.3 (background-jobification + quality fixes) and v3.19+landing batches, plus: real Stripe products created in live mode, the centerdeep VPS prod stand-up at `meeting-ops.unicorncommander.ai`, hosted streamable-HTTP MCP at `/mcp`, dataintel auth re-pointed to commander Keycloak, and the auth-defer fix that stops backend hiccups from killing live meeting UIs.

### Production infrastructure
- `meeting-ops.unicorncommander.ai` LIVE on centerdeep VPS. Backend + frontend split via Traefik PathPrefix, dedicated `meet-postgres`, single-node Garage (both buckets bootstrapped + R/W/O keyed).
- DNS via Cloudflare API; TLS via DNS-01 ACME through existing centerdeep Traefik.
- Center Deep stack archived (11 containers stopped, volumes preserved). MinIO retired.
- Dataintel re-pointed to commander `uchub-keycloak`. New `meeting-ops-prod` + `dataintel-prod` OIDC clients created via Keycloak admin API.
- mem_limit + cpus retrofit on 16 unbounded centerdeep co-tenants.
- Keycloak SMTP fix (port 465 SSL → port 25 plaintext to internal `smtp2graph`).

### Stripe live mode
- Products created via Stripe API: Meeting-Ops Pro $12 (`price_1TcbE0...`) + UC Suite $25 (`price_1TcbE1...`).
- Webhook endpoint registered at `meetingops.magicunicorn.dev/api/stripe/webhook`.
- Suite tier mapping in `services/stripe_client.py`.
- `STRIPE_ALLOW_PROMO_CODES` env gate.
- DEV_TEST_2026 internal coupon (100% off, max 5 redemptions).

### Hosted MCP for external AI clients
- Streamable-HTTP `/mcp` on both bigboy + VPS. PAT-based auth, per-user scoping.
- Same FastMCP instance for stdio + HTTP transports (canonical in `backend/services/mcp_app.py`). Existing Claude Desktop user configs unaffected.
- 16 tools (8 read + 6 propose/confirm/cancel from v3.13).
- `docs/mcp-hosted.md` with snippets for Claude Desktop, Cursor, Continue, Zed, Cline.
- 4 FastMCP gotchas documented in commit body (non-reentrant lifespan, mount root_path, DNS-rebinding gate, CORS bypass for arbitrary clients).

### Auth-defer fix
- `AuthContext` suppresses logout-on-401 when `localStorage.activeRecordingSession` or `alwaysOnRecorderState` indicates a live recording. New `authStaleDuringRecording` context field for future banner UI.
- Caught after Aaron lost a meeting mid-v3.18.3 deploy cascade — each backend restart briefly 401'd `/api/auth/me` → forced redirect → tab destruction.

### Public invite-only landing
- New `frontend/src/pages/Landing.tsx` (hero + 3 product points + invite form). `VITE_LANDING_PAGE_ENABLED=true` on VPS, `false` on bigboy.
- `POST /api/landing/invite-request` (alembic 035), rate-limited, idempotent, Postmark notify.

### v3.18.3 background-jobification
4 handlers (recording finalize, digest gen, TTS summary, TTS podcast) now return 202 + `{job_id, status_url}`; arq workers do the LLM/TTS work out-of-band; frontend polls `GET /api/jobs/{job_id}` with 2s→30s backoff. Closes Cloudflare 524 risk on those paths.

### v3.18.3 quality fixes
- Router prefix double-mount bug closed (simple_settings, ai_settings, agent_management, unified_agent now reachable).
- `RECORDINGS_DIR` env honored in `working_audio_service.py`.
- `audioop` guarded for Python 3.14 (`audioop_lts` fallback + `/health` degraded flag).
- `ROOM_MODE_ENABLED` kill-switch (default ON for bigboy appliance, OFF for VPS cloud).
- Full before-snapshot drift checks in `agent_write_tools.py`.
- Defense-in-depth tier gates on session_emails / batch_export / 11 speakers write endpoints.


### v3.18.3 — Background-jobification of long-running handlers (Codex Performance Finding S1)

Four HTTP handlers that ran server-side LLM work inline (and could exceed Cloudflare's 100s edge timeout, throwing 524 mid-meeting) now enqueue an arq job and return 202 + `{job_id, status_url}` immediately. The frontend polls `GET /api/jobs/{job_id}` on exponential backoff (2s → 30s) until the worker finishes.

- **New `services/job_runner.py`**: thin wrapper over the existing arq Redis pool (reuses `workers.bulk_import_worker.get_arq_pool`). `enqueue_job(function_name, *args, **kwargs)` returns the arq job_id; `get_job_status(job_id)` maps arq's `JobStatus` enum to `pending|running|completed|failed|not_found`. In-process 5-minute idempotency window swallows fast double-clicks.
- **New `GET /api/jobs/{job_id}`** (router `api/jobs.py`): generic poll endpoint, auth-required, 404 on not_found.
- **New worker modules** registered on `WorkerSettings.functions`:
  - `workers/finalize_workers.py:finalize_session_job` — always-on finalize: summary + insights + Brigade + Project-Ops fan-out
  - `workers/digest_workers.py:generate_digest_job` — time-window digest map-reduce (up to 11 LLM calls)
  - `workers/tts_workers.py:render_tts_summary_job` + `render_tts_podcast_job` — TTS render
- **Handlers that now return 202** (instead of awaiting in-request):
  - `POST /api/recordings/sessions/{id}/finalize` (always-on stop)
  - `GET /api/digests` (cache-miss + `force=true` paths; cached digest reads stay 200)
  - `POST /api/sessions/{id}/tts/summary` (cache-miss; cache-hit stays 200)
  - `POST /api/sessions/{id}/tts/podcast` (cache-miss; cache-hit stays 200)
- **Alembic `035_processing_job_id`**: adds `recording_sessions.processing_job_id` (indexed) + `meeting_digest.generation_job_id` for worker drift checks.
- **Drift safety**: worker re-reads the row at entry and compares its ctx `job_id` against `processing_job_id` / `generation_job_id`. Mismatch → skip side effects (no double-writes to Brigade / Project-Ops, no overwriting a fresher summary).
- **Free-tier guards stay BEFORE the enqueue** — free users still see 403, never 202. Validated by `test_finalize_job_flow.py:test_free_user_still_blocked_before_enqueue` and `test_digest_job_flow.py:test_free_user_still_403s_before_enqueue`.
- **Frontend `services/jobPoller.ts`**: `pollJob<T>(jobId, opts)` with exponential backoff to 30s cap, AbortSignal support, 404/5xx handling. `Digests.tsx` migrated to the new flow; `/finalize` callsite in `AlwaysOnContext.tsx` is already fire-and-forget so the 202 is transparent there.
- **Inline-fallback**: every modified handler keeps the legacy in-band path as a fallback when arq is unavailable (test env without Redis, `ARQ_ENABLED=false`). The 202 + poll path is the production behavior; dev keeps working without Redis.
- **Tests**: 19 new (`test_job_runner.py` × 9, `test_finalize_job_flow.py` × 5, `test_digest_job_flow.py` × 5). Pre-existing `test_free_tier_enforcement.py` still 24 pass / 1 skipped / 1 xfailed.
- **Closes**: Codex audit Performance Finding S1. Cloudflare 524 risk window closed for the four named paths.

## [3.19.0] - 2026-05-29

Single coordinated release from a 5-agent parallel swarm closing every UI/UX and revenue-readiness item in PO P-00055 task `622e44b8` acceptance criteria except the launch-day flip itself. All five batches landed independently on `fix/v3.19-*` worktrees, then merged sequentially through `main`; conflicts resolved manually on `Pricing.tsx`, `auth/models.py`, and `SessionDetails.tsx`. 105 frontend + 88 targeted backend tests pass.

### Mobile Free-tier moat (CRITICAL)
- **`MobileLiveRecording.tsx` now gates audio uploads behind `canonical_reprocess` BEFORE `getUserMedia` and BEFORE the recording-start POST.** Free mobile users see a Radix Dialog with the verbatim audit copy explaining mobile-Free is desktop-only today + Pro for mobile server completion. No mic stream, no session POST, no chunk uploads for free users on mobile — the moat violation that originally let the "audio never leaves your device" pitch be technically false on the device most signups arrive from.

### UX rebuild (v3.19 frontend batches)
- **Settings 4-tier IA**: My preferences / Recording defaults / Workspace settings / Admin & appliance. Existing 9 panels regrouped; 8 `<EmptyPanel>` placeholders mark future panels (notifications, hotkeys, recording-defaults, calendar-sync, sharing-retention, speaker-library, audit-export) and reserved the "integrations" slot. New `ThemeSettings` (System / Dark; Light suppressed as undertested).
- **Pricing page**: real Free ($0) / Pro ($12/mo) / Enterprise (Custom) copy replacing placeholder text. Subscribe button wires to `/api/billing/checkout`. Anonymous click redirects to `/signup?return_to=/pricing`. Logged-in users get a "View existing subscription" portal link.
- **Onboarding**: 4-card first-run checklist on Dashboard (test mic / record sample / privacy mode / Pro completion) with per-card localStorage dismissal. `SettingsEnhanced` honors `?section=` and `#audio-devices` deep-links from the checklist.
- **Conference room wizard**: persists visibility + recording mode via `schedule_json`; satellite source enabled with deep-link to `/rooms/{id}#satellite-pair`; always-on + scheduled modes enabled; mock `roomsApi` fallback gated behind `VITE_USE_ROOM_MOCKS` (default off).
- **Speaker library**: respects backend `next_offset` (was hard-capped at 50). "Load more" button + dedup-by-id on append. Height cap from v3.15.1 preserved.
- **SessionDetails IA tabs**: desktop borrows the mobile sticky-pill pattern (custom strip — Radix Tabs not in deps and not worth adding for one consumer). 6 tabs: Summary / Transcript / Action items / Speakers / Attachments / Chat. Brigade 3D graph + TTS podcast + progressive timeline + generated emails collapsed under a "More for this meeting" toggle inside Summary. AI Chat lifted from sidebar to dedicated Chat tab.
- **Upgrade copy on all 6 audit-flagged touchpoints**: server completion (`AlwaysOnControl`), mobile recording (`MobileLiveRecording` — also the moat fix), server live transcript (`LiveRecording`), bulk import (`ImportFilePickerStage`), Brigade graph (`SessionDetails`), cross-device sync (`SessionDetails`). Verbatim audit copy where given.
- **Component library on Radix Dialog**: new `components/ui/Dialog.tsx` is the single modal primitive. `ConfirmModal` refactored on top (API preserved) with the destructive-tone default-focus on Cancel (was Confirm). PAT modals + SpeakerLibrary assign/merge/create modals migrated. `window.confirm` calls removed at `SpeakerLibrary.tsx:334,352` + `SessionDetails.tsx:1954`.
- **WCAG-AA**: `SettingsLayout` sidebar gains `role="tablist"` + `role="tab"` + `aria-selected` + `aria-controls`; `RoomDetail` tabs same. `Sessions.tsx` invalid nested-button markup unwound. `RAGChat` Clear button gains `aria-label`. `ConfirmModal` autoFocus moved to Cancel for destructive flows.
- **`UpgradeBanner` component**: shown to free-tier users at the top of `Sessions` + `SessionDetails`. 7-day localStorage dismissal.

### Per-org integration toggles (Workspace → Integrations)
- **`organizations.integrations` JSONB column** (alembic `v3_19_0_integrations`) holds per-app config: Brigade / Project-Ops / Contact-Ops / Accounting-Ops / Stable. Schema: `{enabled, api_base_url, api_key_encrypted, ...integration-specific fields}`.
- **API keys encrypted at rest** via the existing `services.providers.crypto` Fernet helper. GET endpoints mask (only `has_api_key: bool` + `api_key_last4: str`). No plaintext keys in logs. `/test` endpoint exception path is sanitized.
- **REST**: `GET /api/integrations/me`, `GET|PUT|DELETE /api/integrations/me/{integration}`, `POST /api/integrations/me/{integration}/test`. PUT requires org admin role.
- **Brigade + Project-Ops consumer refactor**: when `Organization.integrations.{brigade,project_ops}.enabled == false`, the respective writer is a no-op for that org EVEN IF the global env var is set. Backwards-compat: orgs with no `integrations` block continue reading the env-var defaults exactly as before.
- **Frontend `IntegrationsPanel`** replaces the placeholder in Workspace → Integrations. Enable/disable toggle + cred input + "Test connection" button per integration. Admin-only.

### Stripe Subscriptions + Founders 100 + signup activation
- **Stripe webhook handler** at `POST /api/stripe/webhook` (alembic `033_stripe_billing` migration: `users.stripe_customer_id`, `users.is_founder`, `organizations.stripe_subscription_id`). Signature verified via `stripe.Webhook.construct_event`. Idempotency enforced via Redis `SET NX EX 86400` on `event.id`. Falls through with warning if Redis unavailable. Handles `customer.subscription.created/updated/deleted` → tier mapping in `User.tier`.
- **Billing API**: `POST /api/billing/checkout` (returns Stripe Checkout URL), `POST /api/billing/portal` (Customer Portal URL), `GET /api/billing/subscription` (current tier + period_end + cancel_at_period_end).
- **Founders 100**: fires at `/api/auth/register` completion AND at first `customer.subscription.created` webhook. Cap respected via DB count vs `FOUNDERS_100_LIMIT`. Pricing unchanged — `is_founder=True` is access + ecosystem bundle eligibility, NOT a discount per Aaron's locked decision.
- **Belt-and-suspenders live-key guard**: `services.stripe_client._stripe()` refuses any key not starting with `sk_test_` unless `STRIPE_ALLOW_LIVE=1` is ALSO set. NO live keys committed.
- **Inert until activated**: billing endpoints return 503 + webhook 503s until `STRIPE_API_KEY` + `STRIPE_WEBHOOK_SECRET` + `STRIPE_PRO_PRICE_ID` are set in `.env.bigboy`. Zero customer impact in absence of those vars.
- **Auth `UserResponse` returns `is_founder` and `has_stripe_customer`** so the frontend renders the Founders badge / billing UI without a second round-trip.

### Mostly-shared infrastructure mistake we documented
The first v3.19.0 deploy to bigboy looked healthy but was running stale code because the `git pull` ran from `/srv/meeting-ops/` (project root, no `.git`) and silently failed; the follow-on rebuild used unchanged source. Caught by route-table introspection (`/api/integrations/me*` paths missing). Memory pointer `feedback_bigboy_git_pull_path` captures the gotcha: pull from `src/`, compose from root, verify route table after every deploy.

### Tests
- Frontend: 105/105 pass after Sessions test-mock added `useAuth` stub for the new `UpgradeBanner` consumer.
- Backend (touched suites): 88 pass, 1 skip, 1 xfail. Includes new `test_integrations_api`, `test_brigade_per_org_toggle`, `test_stripe_webhook`, `test_billing_api`, `test_founders_100`.

### Activation env (set in `.env.bigboy` to enable Stripe + Founders)
```
STRIPE_API_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRO_PRICE_ID=price_...
STRIPE_ENTERPRISE_PRICE_ID=
STRIPE_ALLOW_LIVE=          # only set to 1 on explicit live deploy
FOUNDERS_100_ACTIVE=true
FOUNDERS_100_LIMIT=100
ALLOW_REGISTRATION=true      # to flip public signup on (current dev: off)
```

### Deferred
- Public signup flip + `ALLOW_REGISTRATION=true` on dev → prod cutover.
- VPS prod stand-up at `meeting-ops.unicorncommander.ai` per `docs/deploy-unicorncommander-vps.md`.
- v3.18.3 backlog (background-jobification, agent drift checks, prefix bug, etc.) still queued.


## [3.18.2] - 2026-05-29

### Security — generative-endpoint tier gating
Continues the v3.18.1 moat restoration. v3.18.1 closed the audio-upload + server-recording paths; v3.18.2 closes the remaining server-LLM-on-content paths called out in the Codex code audit table.

- **`/api/ai-chat/sessions/{id}/messages`** (`backend/api/ai_chat.py:231`): per-session AI chat runs server LLM with meeting transcript as context. Now `gate_feature_for_caller(current_user, "canonical_reprocess")` rejects free users with 403 before any work starts.
- **`/api/ai-chat/rag/query`** (`backend/api/ai_chat.py:496`): cross-meeting RAG calls the server LLM over retrieved transcript chunks. Same gate.
- **`/api/simple/recording-sessions/{id}/insights`** + **`/insights/regenerate`** (`backend/api/ai_insights.py`): the gate sits just before `_generate_ai_insights` so cached reads stay open for any tier (cached insights only exist if a paid run generated them earlier) while new generation is paid-tier.
- **`/api/digests`** (`backend/api/digests.py:222`): the gate sits between the cache-check and `_generate_digest`. Cached digest reads stay open; new digest generation is paid-tier.

### Performance — async event-loop hygiene
- **`backend/api/ai_insights.py`**: 6 `svc.chat_sync(...)` calls inside async helper functions (`_generate_summary`, `_extract_keywords`, `_analyze_speaker_sentiment`, `_analyze_overall_sentiment`, `_extract_action_items`, `_extract_decisions`) were blocking the asyncio event loop for tens of seconds per LLM round-trip. Each is now wrapped in `await asyncio.to_thread(svc.chat_sync, ...)` so the loop stays responsive to other requests while the LLM runs. Test guard ensures every `chat_sync` is paired with a `to_thread` wrap (regression catches a future refactor that re-introduces a bare sync call inside an async handler).

### Tests
- `backend/tests/test_free_tier_enforcement.py` (+5 tests): ai-chat messages 403, ai-chat rag/query 403, insights regenerate 403, digest generation 403 (free + force=true with sessions present), and the chat_sync→to_thread regression guard.
- `backend/tests/test_session_watchdog.py` (+2 tests): double-run idempotency (a second consecutive pass over already-failed sessions marks zero new rows), and `SESSION_WATCHDOG_MAX_PER_PASS` cap behavior.

### Audit context
- Findings from the 2026-05-29 Codex code audit, sections "Tier-Gating Audit" + "Performance Findings" (S1). PO P-00055 task `622e44b8-6f9e-4f5c-aa66-d4701cb9794f` tracks the v3.18.x sequence.

### Deferred to v3.18.3
- Background-jobification of long handlers — `api/recording.py:840` (always-on finalize), `api/digests.py:_generate_digest` (LLM chain), `api/tts.py:214,415` (sync render). These need arq job IDs + status-polling endpoints + frontend integration; bigger refactor than fits cleanly here.
- Full before-snapshot drift checks in `services/agent_write_tools.py` (S2).
- Tier gates for `api/session_emails.py` + `api/batch_export.py` + `api/speakers.py` (defense-in-depth; not S0/S1).


## [3.18.1] - 2026-05-29

### Security — moat restoration
- **`/api/uploads/start` transcribe action now tier-gated** (`backend/api/uploads.py`). Free-tier users could previously POST `action="transcribe"` with the 500MB/month quota and trigger the full server STT/diarization/summary pipeline — a moat violation. Now `gate_feature_for_caller(current_user, "canonical_reprocess")` rejects free users at 403 before any work starts.
- **`/api/simple/recording-sessions/{id}/start` tier-gated** (`backend/api/simple_recording_db.py`). Legacy server-recording start triggered STT/diarization/summary on stop. Free is browser-only.
- **`/api/rooms/{id}/recordings/start` tier-gated** (`backend/api/rooms.py`). RoomRecorder posts chunks via `InternalServiceCaller` (legitimately bypassing the gate once running), so we gate at start to prevent free orgs from kicking off the server pipeline.
- **Satellite `/upload-audio` and `/transcript` endpoints now tier-gated by org plan** (`backend/api/satellite_api.py`). Satellites authenticate by device secret OR user session and resolve to an owning org. Free-plan orgs cannot use satellite capture even with a valid device secret. New `_gate_satellite_for_org` helper reads `Organization.plan` (the actual schema column — `Organization` has no `tier` column).
- **`backend/auth/tier.py`: added `get_org_tier(org)` helper** that reads `Organization.plan`, mirroring `get_user_tier` for org-keyed gates. Defaults to `"free"` when missing/None.
- **`backend/api/websocket_remote_audio.py`: WebSocket org/tier authorization**. Previously any valid JWT could connect to any `session_id` UUID (cross-org audio injection). Now: tier-gates on `canonical_reprocess` before `accept()` (close 4403), org-scopes the session lookup via the user's `UserOrganization` membership set (superusers bypass), and returns 4403 on both cross-org miss and not-found (avoids existence-leak).
- **`backend/api/live_transcription.py`: admin check fix on all 3 endpoints**. Previously called `current_user.get("is_superuser", False)` on a SQLAlchemy `User` object — that's a dict method on an ORM object and raised `AttributeError` → 500 instead of 403. Replaced with `not current_user.is_superuser` on start/stop/configure (all three had the same bug).
- **`backend/services/always_on_recorder.py`: org/user injection on `_start_new_meeting()`**. Previously inserted `RecordingSession` rows without `organization_id` or `user_id` (NOT NULL columns). Added `attach_owner(org_id, user_id)` instance method, owner injection in `_start_new_meeting()`, and an early skip-with-warning when both are still `None` at insert time. State machine untouched. `backend/api/simple_recording_db.py:start_always_on` now calls `attach_owner()` immediately after `start()`.

### Security — unauth admin surface lockdown
- **`backend/api/simple_settings.py`, `ai_settings.py`, `agent_management_api.py`, `unified_agent_api.py`**: added `Depends(get_current_user)` + `is_superuser` checks to mutation endpoints (defense-in-depth — a pre-existing prefix double-mount bug in `main.py` made these routes unreachable from outside the container, so the auth changes will engage when that bug is fixed in a follow-up PR).
- **`backend/api/meeting_intelligence_real.py` removed from `main.py` router includes**. Had sync `requests.post(..., timeout=30)` calls that blocked the asyncio loop, no auth, and duplicated functionality covered by `services/providers/impl_llm.py`. File preserved on disk for history.

### Compose hygiene
- **`deploy/bigboy/docker-compose.bigboy.yml` + `.env.bigboy.example`**: added 26 missing env pass-throughs to both `meet-backend` and `meet-bulk-import-worker` (`GARAGE_AUDIO_BUCKET`, `SESSION_WATCHDOG_*`, `ALWAYS_ON_AUTO_SEGMENT`, `MEDIA_RETENTION_*`, `APP_BASE_URL`, `APP_PUBLIC_URL`, `MEETING_RAG_GRAPH_AUGMENTATION`, `PROJECTOPS_*`, `BRIGADE_*`, `STT_DEFAULT_PROVIDER`, `WHISPER_SERVER_URL`, etc.) — closes the env-file-vs-YAML gotcha that caused silent default-value boots. `.env.bigboy.example` updated to match. Build context paths intentionally unchanged (`../../src/backend` is correct for the bigboy symlink-src deploy layout per the "Cloud deployment (bigboy)" section above).

### Tests
- `backend/tests/test_v3_18_1_authz.py` (new, 6 tests): live_transcription 403-not-500, always_on owned/unowned session creation, ws_remote_audio cross-org + free-tier + unauth rejection.
- `backend/tests/test_free_tier_enforcement.py` (+7 tests): uploads transcribe, simple_recording start, room recording start, satellite-via-`get_org_tier`, plus `meeting_intelligence_real`-not-mounted and unauth admin routers xfail (citing the unreachable-prefix bug).

### Audit context
- All findings sourced from the 2026-05-29 Codex code + UI/UX audits. See `~/Documents/meeting-ops-codex-code-audit.md` for the prompt template, and PO P-00055 task `622e44b8-6f9e-4f5c-aa66-d4701cb9794f` for the full v3.18.1 + v3.18.2 + prod-launch plan.

### Pre-existing follow-ups (NOT in v3.18.1)
- Router prefix double-mount bug (`simple_settings`/`ai_settings`/`agent_management`/`unified_agent`).
- `services/working_audio_service.py` hardcodes `/srv` paths.
- Python 3.14 + `audioop` removal blocks `api/streaming.py` import on dev machine.
- v3.18.2: background-jobification of long handlers (`recording.py:840`, `digests.py:107`, `tts.py:214,415`), `ai_insights.py:292` async fix, full before-snapshot drift checks in `agent_write_tools.py`, remaining tier gates for `ai_chat` / `ai_insights` / `digests` / `session_emails`.


## [3.17.0] - 2026-05-28

### Added
- **Project-Ops action-items bridge (ships dark; opt-in per deployment).** When a meeting completes (reprocess or live finalize), Meeting-Ops's existing `action_items` rows are written as Project-Ops tasks on a per-org PO project, modeled on the proven `services/brigade_writer` pattern (best-effort, swallow-all, never blocks the meeting pipeline).
  - `services/projectops_client.py` — httpx Bearer-JWT client to PO NestJS API at `PROJECTOPS_BASE_URL` (default `http://project-ops-backend:3201/api/v1`). Resolves `projectNumber → UUID` by paging `GET /projects` client-side because PO exposes no by-number REST route. `is_live` gate; `create_task` and `update_task_status`; no-op when unconfigured.
  - `services/projectops_writer.write_action_items_to_projectops` — orchestrator with target resolution order: (1) session-level PO link (`session.project_app=project-ops` + `session.project_id` UUID), (2) `session.processing_metadata.po_project_override`, (3) org-level `Organization.settings.projectops_default_project_number`, (4) skip (`mode=no-target`). Idempotent via `raw_payload` stamp on each action_item (`po_task_id`, `po_project_number`, `po_synced_at`); replay never duplicates.
  - Hooks in `api/recording.py` on both reprocess finalize and live finalize. `action_items` PATCH propagates done → COMPLETED via PO `update_task_status` when the row carries `po_task_id`. Every PO call is swallowed.
  - `scripts/backfill_action_items_to_projectops.py` — idempotent reconcile over completed sessions; `--set-default-project ORG=P-XXXXX` helper for the org-level target setting.
  - 8 new tests + the 12 existing Brigade-writer tests still green.

### Activation
- The bridge is **dark** until you mint a Keycloak service-account JWT for a Project-Ops ADMIN/MANAGER identity, set it as `PROJECTOPS_API_KEY` on `meet-backend`, optionally override `PROJECTOPS_BASE_URL`, then set the per-org target with `docker exec meet-backend python3 scripts/backfill_action_items_to_projectops.py --set-default-project <orgid>=P-XXXXX`.

### Future
- Per-org integration toggles (UI for enabling Brigade / Project-Ops / Contact-Ops / Accounting-Ops per org instead of by env-var) tracked as a v1.1 follow-up.


## [3.16.0] - 2026-05-28

### Fixed
- **Graph-augmented retrieval — tuned magnitudes + direct-text dominance rule, closing the v3.11.0 regression.** Live A/B on prod data (4 anchor queries, 80-meeting corpus, 477-node graph): `automatic summarization manual button transcription` no longer demotes the title-matching meeting (the regression case); 3/4 queries reorder usefully with the graph engaged on 4/4. Specific changes in `backend/services/graph_augmented_retrieval.py`:
  - `_MAX_GRAPH_BONUS` reduced 3.0 → 1.5 (now sits below the v3.10.1 `_title_boost` ceiling of 2.5).
  - `_meeting_bonus` per-component values halved: 2.0/1.25/1.0/0.5 → 1.0/0.6/0.5/0.25.
  - New `_title_boost()` mirrors `SemanticSearchService._title_boost` so the rerank composer sees the same direct-text signal as the seed retriever.
  - New `_final_score()` applies a direct-text dominance cap: `title_boost >= 1.0` caps graph_bonus at 0.25; `>= 0.5` caps at 0.5; no direct-text signal lets the full graph bonus through (up to 1.5).
- Three new tests cover the regression (`test_exact_title_outranks_graph_sibling`), the no-direct-text case (`test_graph_signal_still_works_without_direct_text`), and magnitude bounds.
- New `scripts/eval_graph_rag.py`: idempotent A/B harness over the 4 anchor queries; prints a paste-ready comparison table.

### Operational note
- `MEETING_RAG_GRAPH_AUGMENTATION` env var default remains `"0"` (flag off). With the regression fixed and the live eval clean, flipping it to `"1"` on bigboy is now a safe default-on. Per-request `scope.graph_augmented_retrieval=true` continues to override.


## [3.15.1] - 2026-05-28

### Fixed
- Speaker Library layout: the v3.15.0 "Unassigned voice fingerprints" section rendered as a single full-height list outside the page's `overflow-hidden` grid, so a long unassigned list (e.g. 35 rows) pushed the enrolled-speakers two-column grid entirely below the fold with no way to reach it. The section is now **collapsible** (chevron toggle, default open) and its inner list is height-capped (`max-h-96`) with internal vertical scrolling — so however many unassigned fingerprints you have, the enrolled speakers + detail panel below stay in view.

## [3.15.0] - 2026-05-28

### Added
- **Speaker Library — unassigned voice fingerprints + speaker merge.** Closes the gap where diarized clusters (raw `SPEAKER_00` labels) from across meetings couldn't be seen or assigned globally — previously only the per-session inline tagger surfaced them.
  - `GET /api/speakers/unassigned-links` — paginated, org-scoped list of `SpeakerSessionLink` rows where `speaker_id IS NULL`, joined with session context (title, date, duration aggregated from `transcript_diarized` segments, preview text, segment count) and a `top_matches` array (up to 3 enrolled speakers ranked by cosine similarity to the cluster's centroid derived from inline segment embeddings). Verified live: real "Incoherent Transcript Analysis" cluster correctly suggests Aaron Stransky at 0.50 over David (0.18) / Ricky (0.12).
  - `GET /api/speakers/voice-samples/{sample_id}/audio` — proxy for `SpeakerVoiceSample.audio_path` (gated on `STORE_SPEAKER_AUDIO=true`).
  - `POST /api/speakers/{target_id}/merge` — transactional bulk-repoint of `SpeakerVoiceSample` + `SpeakerSessionLink` rows from source to target, recompute target centroid, delete source, audit row (`action="speaker_merge"`, `details={merged_from, source_name, samples_moved, links_moved}`). Refuses `target == source` (400) and cross-org (404). Admin-only. Rollback + 500 on any failure.
  - `services/speaker_service.cosine_similarity` (shared helper).
- **Frontend** (`SpeakerLibrary.tsx`): "Unassigned voice fingerprints" section with count badge, per-row session metadata, suggested-match chips (one-shot assign), and an "Assign…" dialog with searchable speakers dropdown + "Create new speaker + assign" path. Reuses the existing `PATCH /api/sessions/{id}/speaker-links/{link_id}` endpoint. "Merge into another speaker…" action on speaker detail with a rose-accent destructive modal that requires typing the source speaker's display name before Confirm enables.

### Verified
- Full backend suite green; new `tests/test_speakers_unassigned_and_merge.py` (20 pass); frontend vitest (105 pass / 20 files); `npm run build` clean. **Live on bigboy**: routes registered; org-1 baseline is 35 unassigned voice fingerprints across 44 total links, 4 enrolled speakers — Aaron can now see and assign all 35 from the Speaker Library page.

### Architecture note
- This is a direct admin tool, not an agent action — does NOT route through `services/agent_actions.py` propose → confirm → mutate. Per-session inline tagger and the global Library now both reach the same backend mutation surface (`PATCH /api/sessions/{id}/speaker-links/{link_id}`).

## [3.14.1] - 2026-05-28

### Added
- **Typed-confirmation input on the in-app proposal card.** Closes the v3.14.0 gap where `delete_session` (and any future high-friction action) couldn't be confirmed through the RAG chat UI — the card now renders an input field, surfaces the `confirmation_instructions` text, switches to a red/destructive visual treatment (alert icon, rose accent, "Destructive action proposal", "Confirm permanent deletion" button label), and keeps the Confirm button disabled until the user's input matches `required_typed_confirmation` exactly (trimmed). The standard non-destructive card path is unchanged. Test coverage in `__tests__/AgentActionProposalCard.test.tsx`.
- Type system updated: `AgentActionName` now includes `delete_session` / `start_recording` / `stop_recording`; `AgentActionProposal` carries optional `required_typed_confirmation` + `confirmation_instructions`. `confirmAgentAction` accepts an optional `typedConfirmation` arg and only sends `typed_confirmation` in the request body when non-empty (clean wire shape for the common path).

### Verified
- Frontend build (vite + tsc strict) passes. Deployed bundle contains the new behavior strings ("Destructive action proposal", "Confirm permanent deletion", "typed-confirmation", "Confirm stays disabled until").

## [3.14.0] - 2026-05-28

### Added
- **Three new agent write actions, on the v3.12.0 propose → confirm machinery:**
  - **`delete_session`** with high-friction typed-confirmation. The proposal envelope now includes `required_typed_confirmation` (= `"delete-{id}"`) and human-readable `confirmation_instructions`; the user (or chat UI / MCP client) must echo the exact string in `typed_confirmation` on the confirm call, or it 409s. Propose itself refuses while the session is currently recording. Applier cascades: local audio file unlink + `session_media.purge_session_media` (Garage prefix delete) + transcript rows + session row.
  - **`start_recording`** — wraps `WorkingAudioService.start_recording`. Refuses to propose if the session is already recording / completed / processing; applier revalidates status hasn't drifted before invoking the audio service.
  - **`stop_recording`** — wraps `WorkingAudioService.stop_recording`; refuses unless current status is `recording`; sets `status=processing`, `ended_at`, `duration` on apply.
- `typed_confirmation` plumbed through `confirm_action` and `AgentActionConfirmRequest` (only consulted for actions whose builder set `required_typed_confirmation`; ignored otherwise). `_proposal_response` now forwards the optional friction fields (`required_typed_confirmation`, `confirmation_instructions`) so the chat UI / MCP envelope sees them.
- `tests/test_agent_write_tools_v16.py` — 9 cases: typed-confirm positive + missing + wrong-value + recording-blocked; start/stop happy path + status-blocked; reuses the `_set_fake_redis`/`_seed_user_org`/`_seed_session` helpers.

### Verified
- Full backend suite green. **Live on bigboy**: created a throwaway session, propose returned `required_typed_confirmation="delete-126"`, confirm without typed → 409 + session preserved, confirm with wrong typed → 409, confirm with correct typed → applied and session **gone**.

### Note for the Stable voice arc (parallel investigation)
- The home-ops voice + barge-in stack lives at `/Volumes/Studio Storage/Development/smart-home-agent/Home-Ops/voice/` (voice-router + wake-word service) — the reference pattern to lift into Stable when the Stable voice work picks up.

## [3.13.0] - 2026-05-28

### Added
- **Phase 2 v1.5 — Personal Access Tokens (PATs) + MCP write surface.** The agent can now propose, confirm, and cancel write actions over MCP **as the real user**, not as a shared admin. The shared `MEETING_OPS_USER`/`MEETING_OPS_PASS` login path in the MCP server is deleted entirely (no fallback). Per-user identity at the boundary, RBAC enforced by the existing backend auth.
  - `personal_access_tokens` table (alembic `032`): `mops_pat_` + 32 base32 chars (~160-bit entropy), sha256-hashed at rest, `token_prefix` for UI recognition, `last_used_at`/`revoked_at` for hygiene. Per-user FK with `ON DELETE CASCADE`.
  - `backend/auth/pat.py`: `create_pat` (returns plaintext **once**), `resolve_pat` (looks up via hash, updates `last_used_at`, refuses revoked), `revoke_pat`.
  - `backend/auth/dependencies.py`: the auth dependency now dispatches `Authorization: Bearer mops_pat_...` to `resolve_pat()` **before** JWT decode; JWT/SSO paths are unchanged (zero regression on the web app).
  - `backend/api/personal_access_tokens.py`: `POST /api/auth/pats` (plaintext shown once), `GET /api/auth/pats` (never returns plaintext or hash), `DELETE /api/auth/pats/{id}` (revoke).
  - `mcp/meeting_ops_mcp.py`: reads `MEETING_OPS_PAT` env var and sends as Bearer on every backend call. Adds the v3.12.0 `propose_*` tools (+ `confirm_action` / `cancel_action`) as MCP tools — thin proxies, no MCP-side auth logic.
  - Frontend: Settings → **Personal Access Tokens** (`PersonalAccessTokens.tsx`) — list + create + revoke, copy-once modal for the plaintext.

### Verified
- 42 backend tests for PATs + MCP round-trip + agent-actions + tier enforcement; 99 frontend tests; frontend build passes. **Live on bigboy**: minted a real PAT, resolved to the correct user, proposed `rename_session` via the PAT, confirmed → audit rows carry the **real user_id** (not a shared admin), revoked PAT rejected.

### Architecture note
- Built **without** the UC Commander federation layer; PATs are simpler, ship now, and are forward-compatible with federation later (UC could mint PATs on the user's behalf in a future phase). One MCP server codebase, per-user identity per request via PATs.

## [3.12.0] - 2026-05-28

### Added
- **Phase 2 v1 — safe agent write/control via propose → confirm → mutate (in-app).** The agent can now propose mutating actions; the user explicitly confirms; only then does the backend mutate, atomically, with state re-validated against drift. Every mutating action is auditable. The MCP write surface is deferred to v1.5 once per-user MCP identity is wired — v1 ships through the in-app chat where per-user JWT/SSO identity is already solved (Codex's point that a fixed shared MCP login can't enforce per-user scoping).
  - `backend/services/agent_actions.py`: Redis-backed one-shot tokens (`GETDEL`, TTL 300s) bound to `(user_id, org_id, action, payload_hash, expires_at, proposal_id)`. Scope re-validated on confirm AND cancel. Payload tamper detection via sha256.
  - `backend/services/agent_write_tools.py`: 6 actions (`propose_create_session`, `propose_rename_session`, `propose_add_tag`, `propose_remove_tag`, `propose_trigger_reprocess`, `propose_draft_followup_email`). Each confirm path re-fetches the target row, checks owner/org, and verifies the `before` snapshot still matches before applying — refuses with **409 "state changed, please re-propose"** on drift.
  - `backend/api/agent_actions.py`: `POST /api/agent-actions/{propose,confirm,cancel}` — 400 unknown action / 403 tier/ownership/org / 404 missing-or-replayed token / 409 payload-tamper or state-drift / 410 expired token or invalid precondition.
  - `auth/tier.py`: new tier flags `agent_write_basic` (free+), `agent_write_reprocess` (pro+), `agent_write_email_draft` (pro+). `test_free_tier_enforcement.py` tightened — free tier must deny the two pro+ flags.
  - Audit lifecycle reuses `auth_models.audit_logs` with actions `agent_action_proposed` / `agent_action_confirmed` / `agent_action_cancelled`, tied by `resource_id = proposal_id`. Full `before` / `after` / `diff` / `payload_hash` / `result` recorded in `details` JSONB.
  - Frontend: `RAGChat.tsx` renders the new `AgentActionProposalCard` (preview + diff + Confirm/Cancel) when the agent stream returns `needs_confirmation`. Cribbed from the Crisis Ops `IntakePlanReview` pattern.
  - Graph-augmented retrieval (v3.11.0) path preserved alongside the new write surface.
  - `scripts/verify_agent_actions.py`: ratified safety contract as runnable script (post-deploy sanity check).

### Verified
- 36 backend tests for the agent-action surface pass (round-trip per tool + expired/replayed/cross-org/tier/payload-tamper/state-drift negatives) plus 7 frontend tests + the existing full backend suite. **Live end-to-end on bigboy**: real `propose → confirm` on session 122 mutated the row; both `agent_action_proposed` and `agent_action_confirmed` audit rows landed tied by `proposal_id`; **replay correctly rejected** (one-shot GETDEL).

### Not in v1 (deliberately)
- MCP write tools (deferred to v1.5 — needs per-user MCP identity).
- `delete_session`, `send_email`, `start_recording`, `stop_recording` (each has its own friction/safety model worth designing separately).

## [3.11.0] - 2026-05-27

### Added
- **Graph-augmented retrieval (read-only, feature-flagged OFF).** A new layer on top of the existing meeting-rag loop in `backend/services/graph_augmented_retrieval.py`: Qdrant seed → entity extraction → Brigade FalkorDB neighborhood expansion (via the existing read-only `BrigadeClient.fetch_entity_context`) → re-rank with a capped graph bonus (`_MAX_GRAPH_BONUS = 3.0`) → graph evidence block injected into the `ask_about_meetings` LLM prompt. Wired into `search_meetings_impl` and `ask_about_meetings_impl` only.
- Feature flag: env `MEETING_RAG_GRAPH_AUGMENTATION` (default `0` / off) for the global default; per-request `scope.graph_augmented_retrieval` (or `.graph_augment`) wins over env so the same backend can A/B both modes per call. Safe to deploy off; flip on per-request to evaluate.
- Brigade graph backfilled over all 80 completed sessions (`agent_meeting_ops_canonical` went from ~4 smoke-test nodes to 477) so the corpus the new layer expands into is real, not synthetic.
- `scripts/backfill_brigade_graph.py` (idempotent reconcile via `write_meeting_to_brigade`).

### Verified
- 61 backend tests pass (`test_graph_augmented_retrieval` + `test_cross_org_isolation` + `test_semantic_search_service_retrieval` + `test_search_analytics`); cross-org isolation preserved (the graph bonus is applied after the org-filtered Qdrant query). Live A/B on a real cross-meeting query confirms `type=semantic` → `type=graph_augmented` with bounded score boosts and no regression in the OFF path.

## [3.10.1] - 2026-05-27

### Fixed
- **Cross-meeting retrieval (RAG) was broken** — completed meetings were missing from the Qdrant corpus (stale index: org 1 had 81 sessions but only ~178 points), so `ask_about_meetings` / cross-meeting search couldn't surface the right meeting even by exact title. Root cause was corpus coverage, not org filtering (which was correct). Fixes in `semantic_search_service.py`: reindexed the corpus (org 1 → 81 sessions / 398 points); prefix embedded chunk/summary/title texts with the meeting title so titles contribute to the dense + sparse vectors; title-aware additive score boost so exact/near-exact title matches rank deterministically (applied *after* the org-filtered query — org isolation unaffected); `reindex_all` falls back to `final_summary` (structured/legacy shapes) when `summary` is empty. Verified: session 122 now ranks #1 for both exact-title and topical queries. Single-meeting chat was already correct. (Foundation for the agent-platform arc — see `docs/agent-platform-roadmap.md`.)

## [3.10.0] - 2026-05-27

### Added
- **Self-managing local-disk retention (completes the Garage cutover).** A new `services/media_retention.py` plus a daily Arq cron (`media_retention_task`, 04:30 UTC, on the existing `meet-bulk-import-worker`) keeps local disk in check without manual script runs:
  - `evict_completed_local` — deletes the local working file of completed sessions older than `MEDIA_RETENTION_KEEP_DAYS` (default 7), but only when a byte-size-verified Garage copy exists (verified via list, not HEAD). Reversible — audio re-materializes from Garage on next read.
  - `cap_media_cache` — LRU-evicts the re-materialization cache (`MEDIA_CACHE_ROOT`) down to `MEDIA_CACHE_MAX_GB` (default 20); cache entries are re-fetchable from Garage.
  - Safe by construction: never deletes a Garage object, never deletes a local file without a verified durable copy. Env: `MEDIA_RETENTION_ENABLED` / `MEDIA_RETENTION_KEEP_DAYS` / `MEDIA_RETENTION_STATUSES` / `MEDIA_CACHE_MAX_GB`.
- `media_storage.object_size()` — list-based Garage object-size lookup (HEAD-free) used by retention/eviction to verify durable copies.

### Docs
- `docs/agent-platform-roadmap.md`: recorded the Phase 1 audit results + ratified decisions (use Brigade FalkorDB, strict propose→confirm→mutate writes, graph-augmented read-only retrieval) and the re-sequenced near-term order (fix base retrieval + backfill the graph before graph-augmented retrieval). Corrected the stale "vector-only" note (retrieval is hybrid dense + BM25).

## [3.9.1] - 2026-05-27

### Added
- **Help page: a practical "Using Meeting-Ops" guide** above the existing "How it works" architecture tour. Covers recording (Just me / Me + system audio), Sessions & Folders (tags-as-folders, multi-folder membership), Conference rooms & the multi-mic picker, reading a meeting (transcript / summary / Ask-about-this-meeting chat / export), where recordings are stored (self-hosted object storage, delete-means-delete, free-tier stays in-browser), and a Free/Pro/Enterprise plans-at-a-glance.
- `docs/audio-storage-garage.md`: operator + developer guide for the Garage audio storage (modules, columns, read/write/delete flow, backfill + eviction scripts, bucket provisioning, the HEAD-400 gotcha). Linked from the docs index.

### Changed
- README refreshed: latest tag, implementation status (through v3.9.0), tier model (free-tier live + signup status), and a new "Audio storage & data durability" section.



### Changed
- **Garage cutover — Garage is now the authoritative home for canonical meeting audio.** Local disk becomes a transient working cache that can be evicted once a durable Garage copy is verified. To make this safe, the last audio read path that wasn't Garage-aware — `identify_speakers`' embedding re-extraction fallback (`speaker_service.py`) — now resolves via `session_media.resolve_local_path` (local-first, else pull from Garage). The common inline-embeddings path is untouched (it never reads the wav).
- `scripts/evict_local_audio.py`: the cutover tool. Deletes a session's local audio **only after** confirming the Garage object exists with a byte-size match (verified via list, not HEAD). Idempotent, reversible (bytes stay in Garage; reads re-materialize on demand), and never touches a session without a verified durable copy. Run on bigboy to reclaim local disk for completed sessions.

### Notes
- The write/processing pipeline still writes local first (ffmpeg/STT/diarize need a file on disk), then pushes to Garage — so the bind-mount stays `:rw`; "cutover" means Garage-authoritative + local-evictable, not local-free.
- Re-materialized audio lands in the local media-cache; an LRU cache-eviction policy is a follow-up (fine at current volume).

## [3.8.0] - 2026-05-27

### Added
- **Per-room microphone picker (multi-mic).** A Conference Room can now select one *or several* server-attached mics in one flow, each with a friendly per-source label (e.g. "Podium", "Audience"), with a per-device "Test mic" RMS/peak probe before adding. The room's current sources render in the Settings tab with a status badge (idle/recording/error/disabled) and a remove action. Frontend-only; reuses the existing `/api/rooms/{id}/sources` + `/api/system/audio-devices` endpoints. (merge of `rooms-mic-picker`)
- **Sessions folders (tags-as-folders).** A new Folders grouping mode on the Sessions page: the rail lists each tag as a folder with a session count (plus "All sessions" / "Ungrouped"), clicking filters to that tag, and bulk-select lets you add/remove sessions to a folder. A session can live in multiple folders because folders are just tags (deliberate — a meeting can belong to several). Available on mobile too. The flat list + search + filters remain. (merge of `sessions-folders`)

### Changed
- `GET /api/simple/recording-sessions/tags` now also returns `tag_counts`, `total_sessions`, and `untagged_sessions` (additive — new fields with defaults) so the folder rail renders counts without extra queries.

## [3.7.0] - 2026-05-27

### Added
- **Canonical meeting audio now lives in Garage object storage.** The main meeting audio (the irreplaceable asset) was on a single host's local disk; it now also has a durable copy in the `meeting-ops-audio` Garage bucket, mirroring how session attachments already work. New nullable columns on `recording_sessions` (alembic `031`): `audio_storage_backend` (`garage`|`local`|NULL) and `audio_object_key` (`{org}/{session_id}/audio/{name}`).
  - `services/media_storage.py`: Garage S3 client for the audio bucket — `put_path`/`put_stream`/`open_object`/`iter_object`/`cached_local_path`/`delete_object`/`delete_prefix`, with a local-first spool fallback and a `MEDIA_STORAGE_DISABLED` escape hatch (tests force-local).
  - `services/session_media.py`: `persist_session_audio` (best-effort push + record columns), `resolve_local_path` (local-first, else pull from Garage into cache), `purge_session_media` (delete the whole session prefix).
  - **Writes**: durable copy pushed on always-on finalize, both upload paths, and bulk-import. All best-effort and additive — the local file stays the working copy / source-of-truth; a Garage hiccup just leaves the columns NULL to retry.
  - **Reads**: `download/audio` resolves local-first, else proxy-streams the bytes from Garage (range-capable `FileResponse`; Garage is never exposed to the browser).
  - **Delete**: deleting a session now purges its Garage objects too — so "delete my data" is actually honored (GDPR/CCPA).
  - **Backfill**: `scripts/backfill_audio_to_garage.py` (idempotent, additive) seeded the existing recordings into Garage (15 sessions / ~993 MB on bigboy).
  - Free-tier audio is unaffected (browser-only; never reaches the server, never uploaded).

### Changed
- **Bulk-import Garage upload consolidated** onto `services.session_media`. The previous bespoke `_upload_to_garage` used a job-scoped key (`{org}/{job_id}/{file_id}`) and had been silently failing because the `meeting-ops-audio` bucket never existed; it's replaced by the single canonical session-scoped path so the download fallback + delete-cascade find bulk-imported audio too.

### Fixed
- **Garage `HEAD` returns 400** with this botocore (same family as the `@aws-sdk` checksum/HEAD gotchas). `cached_local_path` now streams via `get_object` instead of `download_file` (which issues a `head_object` first) — fixing the evicted-cache / disaster-recovery read path — and `ensure_bucket` no longer probes with `head_bucket`.

### Ops
- Provisioned the `meeting-ops-audio` Garage bucket and granted the `meetingops` data key read/write/owner (it couldn't self-create buckets — the root cause of the prior silent failures).

### Not yet (gated / follow-ups)
- Local stays the source-of-truth; **no cutover** to Garage-primary or local eviction yet (that's the next, separately-confirmed step). Retention/lifecycle policy per tier, encryption-at-rest (HIPAA path), and TTS-output durability are deferred follow-ups.

## [3.6.1] - 2026-05-27

### Removed
- The "Voice Activity Detection (removes silence)" toggle + threshold from Settings -> Audio. It persisted a preference (`enableVAD`/`vadThreshold`) that **no recording path ever read** — the always-on VAD engine ignores it, and the parallel full-session recorder captures everything continuously regardless of VAD. So the toggle was misleading. A true no-VAD live-transcription mode would be a separate feature (a time-based chunker), not this toggle.

## [3.6.0] - 2026-05-27

### Changed
- **Live Recording page: one summary surface.** Removed the legacy page-level "Meeting Summary (Live AI Updates)", "Key Points", and optional "Live Transcript" panels (driven by the old `/ws/transcription-auto` `autoSummary`/`summaryFormats` path) that duplicated the recorder's own rolling Live summary and got pushed to the bottom of the page. `AlwaysOnControl`'s Live summary is the single live-summary surface now; the structured summary + key points + action items remain in Session Details (post-meeting), and the gated `ServerLiveTranscript` (Pro server-live) stays. (The now-dead legacy summary state + `/ws/transcription-auto` handlers are left as harmless no-ops; `noUnusedLocals` is off so they do not break the build — deeper removal is a follow-up.)

## [3.5.1] - 2026-05-27

### Fixed
- Public pages now reachable at real paths. The app is a HashRouter SPA, so only `/#/pricing` (etc.) rendered; landing on `/pricing` with no hash fell back to `/` -> /dashboard. Added an `index.html` bootstrap that rewrites `/pricing`, `/signup`, `/login` to their hash form before the router mounts, so those URLs are typeable and shareable. (Anonymous access still needs the oauth2-proxy skip-routes per docs/consumer-signup.md.)

## [3.5.0] - 2026-05-27

### Added
- **Consumer signup / login / pricing frontend.** Public `/signup` (email + username + password -> `POST /api/auth/register`, then a "check your email to verify" state), `/login` (email-or-username + password via `AuthContext.login`, the existing enterprise SSO button kept, and verification banners for `?verify=success|invalid` with a resend flow), and `/pricing` (Free / Pro / Enterprise comparison; Pro at a placeholder $16/mo, Enterprise contact-sales). The free-tier upgrade prompt now links to `/pricing`. Routes are public (outside ProtectedRoute); the rest of the app stays auth-gated. Backend was already built (v3.2.0); this is frontend-only and reuses AuthContext for token storage.

### Notes
- Inert for current enterprise users (authed via SSO; /login keeps the SSO button). **Anonymous access to /signup + /login still requires the oauth2-proxy skip-route change + `ALLOW_REGISTRATION=true`** (see `docs/consumer-signup.md`) — that ops activation is the remaining gate before strangers can sign up. /pricing is reachable now for authed users.

## [3.4.1] - 2026-05-27

### Fixed
- Browser tab favicon now uses the branded `icon-192.png` instead of the default Vite logo (`index.html`). Interim until the real logo lands; the PWA/apple-touch icons were already branded.

## [3.4.0] - 2026-05-27

### Changed
- **Record page: one live transcript + a collapsible pipeline panel.** Removed the legacy page-level "Live Transcription" panel — it mirrored the old `/ws/transcription-auto` feed and duplicated the recorder's own live transcript (the code even noted "the user sees BOTH transcripts"). The live transcript now comes only from the active recorder (`AlwaysOnControl`), plus the gated `ServerLiveTranscript` pane for Pro server-live. Collapsed the "Agent & Pipeline Status" panel (per-session STT / diarizer / summarizer / server-vs-browser pickers) behind a default-collapsed "Advanced" toggle, since those are power-user controls most users never touch. Session Info and Quick Actions panels are unchanged.

## [3.3.0] - 2026-05-27

### Changed
- **Record page: one recorder, two capture options.** Removed the redundant "Record from this browser" card (`DesktopBrowserRecorder`) — it duplicated the always-on recorder but uploaded straight to the server to transcribe, bypassing the browser-first / privacy path and the free-tier gate (an un-gated upload hole, now closed). `AlwaysOnControl` is the single recording surface. Collapsed the meeting-type selector from three (In-person / Online / Mixed) to two: **Just me** (microphone only) and **Me + system audio** (mic + screen / window / browser-tab). Dropped the now-redundant sub-toggles; the raw Mic / Tab / Mic+Tab modes remain in the Advanced expander for power users.

## [3.2.0] - 2026-05-27

### Added
- **Consumer self-serve signup backend (Option B), code-complete but INERT until activated.** Email/password signup for the free tier, coexisting with Keycloak enterprise SSO (the backend already dual-trusts Keycloak headers + its own JWT).
  - `AuthService.create_user(personal_org=True)` provisions a PRIVATE per-user org (`{username}-personal`, user = admin) so consumers never share a workspace; new users are `tier="free"` + `is_verified=False`.
  - Self-serve `POST /api/auth/register` uses the personal-org path and sends a verification email (best-effort — a mail failure never fails signup).
  - `GET /api/auth/verify-email?token=…` (flips `is_verified`, redirects to the app) + `POST /api/auth/resend-verification` (generic 200, no account enumeration).
  - `auth/email.py` — transactional email via `auth_config` SMTP; logs the link + returns False when SMTP is unconfigured (never 500s a signup). Email-verification tokens are type-isolated from password-reset tokens.
  - Reuses the existing account lockout (`authenticate_user`) and free-tier default; not rebuilt.
- Tests: `tests/test_consumer_signup.py` (11) — token helpers + type isolation, `create_user(personal_org)` (private org / free / unverified) + legacy default-org path + duplicate rejection, the register glue (personal org + verification email), and the verify-email / resend endpoints.

### Notes
- **Inert until activated**: with `ALLOW_REGISTRATION=false` (default) `/register` still requires an admin and the new endpoints sit behind oauth2-proxy. Deploying changes nothing for current users. Activation (real `SECRET_KEY`, `ALLOW_REGISTRATION=true`, SMTP, oauth2-proxy skip-routes, Traefik rate-limiting) + the frontend `/signup`+`/login`+`/pricing` pages are documented in `docs/consumer-signup.md` and gated on an explicit launch decision.

## [3.1.0] - 2026-05-27

### Added
- **Free-tier wiring: free users now record on-device by default.** The Record / always-on flow (`AlwaysOnContext`) forces local-only mode for users without the `canonical_reprocess` capability (free tier), so a free user records entirely in the browser (on-device STT + LLM + IndexedDB) with zero server-processing calls instead of hitting 403s mid-recording. A free user on a non-WebGPU browser is cleanly blocked with a "use Chrome/Edge" message rather than falling through to server calls. Every server-processing call (chunks / text / finalize / finalize-audio / resume) is guarded by a capability check plus a 403 backstop that surfaces a dismissible upgrade prompt (links to `/pricing` and Local Sessions). The Local Sessions page is now always reachable from the nav.

### Changed
- Privacy Mode + no-WebGPU now blocks recording with a clear message instead of silently falling back to server recording. Previously a paid user with Privacy Mode ON but no WebGPU would record server-side anyway, contrary to their stated privacy intent.

### Notes
- Inert for current enterprise/superuser users — the free-path code only triggers when `canonical_reprocess` is absent. Frontend-only. tsc build clean; 93 frontend vitest tests pass. Two stale frontend tests (config api-subdomain expectation, Sessions fixture) were corrected. The `/pricing` link is a placeholder pending the pricing page. On-device transcript-quality validation (a real browser-only recording) is still a pre-launch gate.

## [3.0.0] - 2026-05-27

### Added
- **Free-tier enforcement: free tier is browser-only, server processing is paid-tier.** Four new `TIER_FEATURES` capabilities (free=False, pro/enterprise=True), mirrored in the frontend `TierFeatures` type + `DEFAULT_TIER_FEATURES`:
  - `canonical_reprocess` — the server audio-ingest + reprocess pipeline.
  - `brigade_integration` — Brigade graph writes (distinct from the enterprise-only `brigade_byok`).
  - `cross_device_sync` — server-backed cross-device sync (capability flag).
  - `bulk_import` — the server batch `/import` pipeline.
- Two gate primitives in `auth/tier.py`: `require_feature(feature)` (FastAPI dependency, 403s users lacking the capability; validates the feature name at boot) and `gate_feature_for_caller(caller, feature)` (inline gate for `get_internal_or_user` endpoints — internal-service callers bypass, real users are 403'd; duck-types on the `tier` attribute so it's robust to the test suite's `auth.models` reload).

### Changed
- **Server-processing endpoints now require a paid tier.** Gated (free → 403): `/sessions/{id}/chunks`, `/sessions/{id}/chunks-text`, `/sessions/{id}/audio-chunks`, `/sessions/{id}/full-audio`, `/sessions/{id}/finalize-audio`, `/sessions/{id}/finalize` (all `canonical_reprocess`); `/summarize` + `/summarize-slice` (`qwen36_summary`); `/import/jobs` + `/import/jobs/{id}/files` (`bulk_import`). The streaming WS `/ws/sessions/{id}/live` was already gated on `server_live` (Phase B.4). Session CRUD and browser-side capture stay open — free tier records locally (on-device STT + LLM + IndexedDB, i.e. Privacy Mode) and never touches the server. Brigade writes + the reprocess pipeline are downstream of the gated finalize/chunks entry points, so free users never reach them.
- **Wire contract widens (the reason for the major bump):** free-tier users now receive explicit `403` on tier-locked endpoints. Internal-service loopback callers (room_recorder) are unaffected — they bypass tier gating.

### Tests
- New `test_free_tier_enforcement.py` (13): the two gate primitives (free 403 / pro+ pass / superuser pass / internal-caller bypass / unknown-feature ValueError) plus integration tests hitting real gated endpoints free-vs-pro. `test_tier.py` key-consistency updated for the 4 new capabilities. Affected functional tests (audio-chunks, bulk-import, chunks-text, cross-org, internal-auth) updated to seed paid-tier users. Full suite minus the bulk-import *worker* test files: 0 failed; the 3 bulk-import files pass in isolation (7 + 7 + 15). Known test-infra issue (pre-existing, orthogonal to enforcement): the bulk-import worker tests share async event-loop/queue state and hang when run after other files in a single process — tracked as a separate follow-up.

## [2.3.0] - 2026-05-27

### Changed
- **PWA updates no longer apply mid-recording.** The service worker moved from `registerType: "autoUpdate"` + workbox `skipWaiting: true` to `"prompt"` + `skipWaiting: false`, so a freshly-deployed SW parks in the *waiting* state and only activates when the user clicks "Reload now" (which sends `SKIP_WAITING` via `updateSW(true)`). The `PWAUpdate` banner is now suppressed while a recording is in progress — live (`RecordingContext.isRecording`) OR always-on capture (`AlwaysOnContext.state` in starting/recording/paused/stopping) — and reappears the moment recording stops with tailored copy ("we held an update while you were recording"). When no recording is active it behaves as before: prompt as soon as the update is detected. Removes the footgun where a deploy could swap cached chunks out from under an active recording.

## [2.2.0] - 2026-05-27

### Added
- **Opt-in Sortformer canonical-hybrid diarization for `/finalize-audio`** (default OFF). A new path where NVIDIA Sortformer draws the speaker boundaries and wespeaker (still via `meet-speaker-svc`) supplies the per-turn voice embeddings used by `identify_speakers`. Selected per-org (`OrgProviderSettings.provider_name = "sortformer-hybrid"`) or globally via `SPEAKER_PROVIDER_PREFERENCE=sortformer-hybrid`. **Default stays `pyannote` — production is unchanged.**
  - `meet-sortformer-svc`: new `POST /diarize-file-upload` — multipart WAV upload (works cross-host; the backend on bigboy and the svc on midboy2 don't share a filesystem), runs Sortformer segmentation, then asks `meet-speaker-svc` for a per-turn wespeaker embedding over the midboy2-local network, and returns the **same response shape** `meet-speaker-svc /diarize` returns (`segments[{start,end,speaker,embedding}]`, `num_speakers`, `backend`, `duration_seconds`). Turns under 0.5 s skip embedding; an unreachable speaker-svc degrades to labels-only and never raises.
  - Backend `SortformerSpeakerSvcProvider`: a drop-in for `LocalSpeakerSvcProvider.diarize`; `embed`/`identify`/`health` delegate to a wrapped wespeaker provider (wespeaker stays canonical for speaker identity — only the segmentation changes). `registry.get_diarization` gates on the preference, per-org override winning over the env default.
  - Tests: 9 backend (registry selection, response parsing, delegation, graceful failures) + 6 sortformer-svc (endpoint shape, `SPEAKER_NN` label mapping, embed crop/skip, unreachable-svc degradation).

### Known limitations
- **The hybrid is NOT viable as the canonical diarizer for real-length meetings yet, and is shipped opt-in/parked.** Sortformer v1 is trained on ~90 s sessions and its one-shot `diarize()` allocates memory roughly quadratically in audio length: a 38-minute meeting needs ~25 GiB and OOMs the 12 GB RTX 3060 (a 24 GB card would not fit it either). It works fine on short clips (validated: synthetic 2-speaker fixture → 2 speakers + 256-d embeddings end-to-end). For comparison, the canonical pyannote path diarized the same 38-min meeting in ~76 s (2 speakers, 694 segments, 577 embedded turns).
- Making it canonical requires either (a) chunked diarization with cross-chunk speaker stitching via the wespeaker embeddings, or (b) Sortformer v1's `forward_streaming_step()` + speaker-cache API (the spike report's promotion path), or (c) NeMo >= 2.5 streaming-v2 models. Deferred by decision 2026-05-27 — pyannote remains canonical; the hybrid stays as opt-in short-audio infrastructure.
- When the hybrid is selected and the diarize call fails (e.g. OOM on a long meeting), `diarize()` returns `[]` and the reprocess pipeline keeps the live transcript with no speaker overlay. There is intentionally no automatic pyannote fallback in this release — keep the flag off for long-meeting orgs.

## [2.1.0] - 2026-05-26

### Added
- **Per-word streaming UI in `ServerLiveTranscript`**: v2.0.0 wired the backend to consume `/transcribe-stream-v2` and surfaced `tokens_finalized` / `tokens_draft` / `eou_detected` on each WS frame, but the frontend kept the old "one transcript entry per 2.5s flush" cadence. v2.1.0 adds an utterance-grouped accumulator (`Utterance[]`) that appends newly-finalized tokens to the trailing utterance as they arrive — closer to how a real-time captioner displays. Each utterance is a paragraph block: speaker badges + per-word render (one `<span>` per token with `title` showing the timestamp + confidence) + faint italic draft suffix on the active utterance + gray EOU chip when the model sealed end-of-utterance. New utterance opens on the next frame after EOU. Capped at 50 utterances in the DOM to keep long sessions responsive; older utterances drop off the top of the rolling pane.
- **Activation is automatic**: as soon as ANY partial frame carries `tokens_finalized` (i.e. STREAMING_USE_V2_PARAKEET=1 and the v2 endpoint is producing per-word stream), the new utterance view replaces the legacy per-frame view. Legacy clients hitting backends without v2 endpoint keep their old rendering (the transcript[] array still populates as a fallback) — no break in either direction.
- New `Utterance` interface + `UtteranceView` component co-located with `ServerLiveTranscript`. Per-word `<span>` carries `title={t.start.toFixed(2)}s-{t.end.toFixed(2)}s conf={t.confidence.toFixed(2)}` so users can mouse-over any word and see its timing without cluttering the visible UI.
- The active utterance shows the in-flight `text_draft` faint italic at the trailing edge so users see the leading edge of what the model is *thinking* before it commits — this is the per-word streaming UX Phase B.3 chunk C originally envisioned.

### Notes
- The latest speakers-array from each frame "wins" for the currently-active utterance. Past sealed utterances keep their speakers from when they were sealed, so historical attribution is stable. If sortformer re-segments mid-utterance (e.g. detects a second speaker entering), the active utterance's badges update; on EOU + next utterance the prior speakers stick.
- DOM diffing is small per frame because most rendered word spans don't change frame-to-frame — only the trailing draft + the new finalized words update. React's reconciliation handles this efficiently for the 50-utterance cap.
- The legacy per-frame transcript view (each partial = its own entry) stays as a fallback for v1-endpoint sessions and is the rendering path before any tokens arrive (e.g. while WS is connecting). When the first tokens-bearing frame arrives, the view automatically switches.

## [2.0.0] - 2026-05-26

### Added
- **True cache-aware streaming WS path is now the production default**: backend `_flush_to_stt` now POSTs to `services/parakeet-stream-svc/main.py`'s `/transcribe-stream-v2` endpoint (built by Agent C in v1.2.0 as a stepping stone) instead of `/transcribe-stream`. Combined with the v1.5.0 model swap to `parakeet_realtime_eou_120m-v1`, this is the realization of Phase B.3 chunk C's original vision — Agent C's spike found the v3 checkpoint blocked cache-aware streaming, so they built the v2 endpoint as a stepping stone; v1.5.0 swapped to a checkpoint that actually supports it; v2.0.0 wires the endpoint up live. Gated by `STREAMING_USE_V2_PARAKEET=1` in `.env.bigboy` + the explicit forward in the backend service env block of `docker-compose.bigboy.yml`. Rollback is a single env flip back to 0 + backend restart.
- **Partial/final JSON frames now carry per-word `tokens_finalized` array**: each WS frame from the live endpoint includes `tokens_finalized: [{word, start, end, confidence}, ...]` (delta of words newly promoted from draft → finalized in THIS frame) plus `tokens_draft`, `text_draft`, and `eou_detected: bool`. The legacy `text` field stays populated (built from the joined finalized words) so v1.4.x clients keep working. New fields ride alongside the existing `segments` / `confidence` / `model` / `rtf` shape — additive, non-breaking.
- **End-of-utterance (EOU) detection surfaced in the live UI**: the streaming-trained model emits an `<EOU>` token when the speaker stops talking. The backend strips the literal token from `text` (so the UI doesn't render it as a word) and surfaces it as `eou_detected: bool` on the outbound WS frame. The frontend `ServerLiveTranscript` renders a small gray "EOU" chip next to entries where the model detected end-of-utterance — visible feedback that the model understands speaker pauses. Works on both v2-endpoint and v1-endpoint paths (the new model emits `<EOU>` either way).
- **In-flight draft tokens visualized**: when `tokens_draft` is non-empty (model emitted speculative words it may revise), the frontend renders `text_draft` in faint italic next to the finalized text. Gives users a sense of the leading edge of the transcript stream without committing them to the un-stable suffix.

### Changed
- **Wire-contract bump for the live WS endpoint**: partial/final frames may now contain `tokens_finalized` + `tokens_draft` + `text_draft` + `eou_detected`. Clients that ignored unknown fields (the v1.x compat path) keep working; clients that want per-word streaming UX should consume the new fields. This is the rationale for the 2.0.0 major bump — wire-format addition, not removal, but a semantic widening of what live WS frames carry.

### Notes
- The v1.3.0 `consumed_through_ms` cursor + `audioop.rms` VAD silence-gate workarounds STAY in place. With v1.5.0's EOU-trained checkpoint they should fire much less often (the model itself recognizes silence), but they're cheap insurance against checkpoint regressions or unusual audio. We can remove them in v2.1.0 if production telemetry shows zero firings over a sustained window.
- The v2 endpoint is session-stateful (maintains a per-`X-Session-Id` audio ring). Backend WS forwarder already passes a stable `X-Session-Id` header per WS connection, so the svc reconstructs the ring correctly across the 2.5s flush cadence.
- Per-word streaming UI in v2.0.0 is intentionally minimal — full per-word render-as-they-arrive (the v2.1.0 polish) requires a richer state model than "one transcript entry per partial frame". For now the entries still snap in at 2.5s cadence; the draft + EOU chips are the visible upgrade.

## [1.5.0] - 2026-05-26

### Changed
- **Streaming STT checkpoint swap: parakeet-tdt-0.6b-v3 → parakeet_realtime_eou_120m-v1**: replaces the live-streaming model on midboy2 GPU 0 with a checkpoint that was actually *trained* for cache-aware streaming. Per Agent C's v1.2.0 spike (`docs/phase-b3-nemo-streaming-spike.md`), the previous 0.6B v3 checkpoint shipped with `att_context_size=[-1,-1]` (full-context attention) which made true streaming impossible — necessitating v1.3.0's `consumed_through_ms` cursor + audioop VAD silence-gate workarounds. The new 120M EOU model has `att_context_size=[70,1]` + `att_context_style=chunked_limited` + `model.conformer_stream_step` working, AND emits an `<EOU>` (end-of-utterance) token in transcripts when the speaker stops talking — directly addressing the v1.3.0 silence-hallucination root cause at the model level rather than at the WS forwarder level. Eval results on the synthetic_2speaker fixture: 5x smaller (114.9M vs 600M params), VRAM at fp16 drops from ~1.7 GiB to **629 MiB** on the RTX 3060, load time 12.85 s warm (was ~25 s), transcribe 5 s audio in 1.58 s cold / sub-second warm. Transcript quality looks clean (`"welcome everyone to today's test meeting i am the first speaker..."`). The v2 endpoint code (`/transcribe-stream-v2`) shipped in v1.2.0 now also works against this checkpoint and returns `tokens_finalized: [{word, start, end, confidence}, ...]` — Agent C's spike vision realized. The backend still routes through `/transcribe-stream` (v1 endpoint) with `STREAMING_USE_V2_PARAKEET=0`; flipping the flag + consuming the per-word stream shape on the backend + frontend is the v2.0.0 follow-up. Fallback model stays `nvidia/parakeet-tdt-0.6b-v2` (multilingual, full-context — unaffected by this swap). Tradeoff: 120M may have lower absolute accuracy than 600M on hard audio, but the streaming-trained behavior + EOU detection are decisively better for the live UX. The canonical /finalize-audio pipeline still uses the 1.1B v3 model on bigboy for the final transcript, so live-mode quality limits don't affect canonical quality.
  - midboy2 compose: `PARAKEET_STREAM_MODEL` default changed.
  - Image: `meet-parakeet-stream-svc:local` rebuilt from current source (includes Agent C's `/transcribe-stream-v2` endpoint code) and shipped bigboy → midboy2 via `docker save | docker load` (~4.5 min over Tailscale).
  - Multilingual note: the EOU model card lists English-focused training data (AMI, Fisher, librispeech). For multilingual sessions, fall back is the v2 model. A future eval should test the v3 multilingual model on non-English audio if we ship that as a runtime option.
  - The v1.3.0 `consumed_through_ms` cursor + VAD silence gate workarounds STAY in place. With the EOU model they should fire less often (the model itself recognizes silence and stops emitting), but they're cheap insurance. We can remove them in v2.0.0 if production telemetry shows they never fire.

## [1.4.1] - 2026-05-26

### Fixed
- **Settings > In-browser AI: clarify what the "Summary model" dropdown actually controls** (Aaron observed selecting Gemma 4 E2B but seeing Qwen 3.6 in the live summary display). The dropdown previously labeled "Summary model" sets the WebGPU/wasm in-browser LLM used only in Privacy Mode or as a browser-only fallback; normal-mode recordings send audio to the server, and the server-side summarizer (env-configured via `MEETING_OPS_LLM_MODEL`, defaults to `Qwen3.6-35B-A3B-Vision` on midboy1 P40) drives the live progressive summaries the user sees. The label now reads "In-browser summary model (privacy mode + browser-only fallback)" with a paragraph explanation directly below. New read-only "Server-side summarizer" panel below the dropdown fetches the actual model from `/api/system/pipeline` and surfaces it as `Currently {Qwen3.6-35B-A3B-Vision} on backend GPU` so users see both sides at a glance. No behavior change — pure UX clarity. Per-org server-side overrides (admin only) still live in Settings > AI Providers and the dropdown will continue to honor /finalize-audio overrides per `transcription_options.llm_model`.

## [1.4.0] - 2026-05-26

### Added
- **Server-live transcript with Sortformer speaker labels now visible inside the production Record page**: refactored the Phase B.2 streaming UX out of the admin-only `/streaming-test` diagnostic and into the actual `/record` page (`LiveRecording.tsx`). New reusable component `frontend/src/components/ServerLiveTranscript.tsx` (~270 lines) owns the WebSocket lifecycle for `/ws/sessions/{sessionId}/live`, runs its own AudioWorklet mic capture via the existing `startPcm16Capture` helper, and renders a transcript pane with the v1.2.1 `<SpeakerBadges>` color palette inline. Drop-in component takes `sessionId` + `enabled` props; teardown / re-init is automatic on prop changes. Tier-gated at the call site via `useTierFeatures().hasFeature('server_live')` — free tier sees no change, enterprise / pro / superuser see a second transcript pane fed by Phase B.2 streaming + Sortformer parallel dispatch (per v1.2.0 / v1.3.0 / v1.3.1). Both pipelines run in parallel during a recording — the existing `/ws/transcription-auto` path keeps providing live captions + auto-summaries + audio-levels, the new `/ws/sessions/{id}/live` path adds the speaker-labeled near-streaming view. Browsers happily share one OS-level mic across multiple `getUserMedia` calls, so the dual-pipeline architecture works without coordination. The admin `/streaming-test` page stays as the diagnostic; this is the production view. Frontend Docker build (tsc -b strict per v1.1.0) succeeds; 5 baseline vite warnings unchanged.

## [1.3.1] - 2026-05-26

### Fixed
- **Sortformer min-segment filter for cleaner speaker badges**: drop speaker turns shorter than `SORTFORMER_MIN_SEGMENT_MS` (default 500 ms) before emitting in the partial JSON. Sortformer over-segments on solo / quiet audio — flagging brief background-noise / breath sounds / mic-handling artifacts as a different speaker for ~100-300 ms (Aaron observed `speaker_1` chips appearing alongside his own `speaker_0` text on the v1.3.0 smoke test). Real conversational turns are almost always > 500 ms (people speak for at least a syllable), so the 500 ms floor catches genuine multi-speaker windows while dropping the noise. Filter lives in `_flush_to_stt` right after the sortformer response parses, so the frontend `<SpeakerBadges>` never sees the noise. `sortformer_distinct_speakers` also clears to 0 if the filter drops every turn, so the UI doesn't show stale counts. Configurable via env (`SORTFORMER_MIN_SEGMENT_MS=0` disables).

## [1.3.0] - 2026-05-26

### Fixed
- **WS partial transcripts no longer repeat + no longer hallucinate on silence**: v1.2.x near-streaming pipeline shipped a 25 s rolling-window approach where each 2.5 s flush sent the entire buffer to parakeet, then parakeet returned a fresh transcript of the whole window. With `nvidia/parakeet-tdt-0.6b-v3`'s full-context attention training, this produced two failure modes during the v1.2.0 smoke test: (1) the user spoke a sentence and saw it repeat 5+ times in the live transcript as the window kept resending the same audio, and (2) when the user stopped talking, the trailing silence in the window degenerated into repeating-token hallucination ("a little bit of a little bit of a little bit of..."). Per Agent C's spike report (`docs/phase-b3-nemo-streaming-spike.md`), the model architecture itself can't be fixed without a checkpoint swap — but the buffer-management half can.
  - **Cursor-based windowing**: added `_SessionState.consumed_through_ms`, advanced after every successful partial / final emission. `take_pcm()` now returns audio from `(consumed_through_ms − STREAM_LOOKBACK_SECONDS)` forward instead of the last 25 s of buffer. Default lookback 1.0 s keeps a small overlap for word-boundary continuity at the seam. Each flush now sees ~2.5 s of NEW audio + ~1 s of overlap, not 25 s of mostly-rehashed audio.
  - **VAD silence gate** (audioop.rms): added `_is_silent()` helper using Python stdlib `audioop.rms(pcm, 2)`. Default threshold `STREAM_VAD_RMS_THRESHOLD=200` catches background-noise-level windows; full-scale PCM16 sine peaks RMS ~23170, typical speech is 1000-5000 RMS, true silence is < 50. When a window is below threshold, parakeet is skipped entirely (no GPU waste, no hallucinated tokens) but the consumed cursor still advances so silence doesn't accumulate as a backlog of re-transcribe targets. Configurable via `STREAM_VAD_ENABLED=0` for diagnostics.
  - **Empty-text / identical-text de-dup paths** also now advance the consumed cursor (previously they only `return`ed, leaving the buffer to grow). This catches the case where parakeet declined to commit tokens on a low-confidence window — we trust its decision and move on rather than retry with the same audio.
  - Recovery safety net: the 25 s max window cap stays as a recovery guard for the case where N consecutive flushes failed (cursor stalled) — prevents shipping 5×N seconds of audio on the next try.
  - Smoke-tested by Aaron on `meetingops.magicunicorn.dev/#/streaming-test` immediately after deploy.

## [1.2.2] - 2026-05-26

### Fixed
- **WS streaming endpoint crashed on every Connect with `code=1006` due to DetachedInstanceError in `_resolve_org_bucket`**: latent Phase B.5 bug surfaced during the v1.2.0 smoke test. `_resolve_ws_user` (in `backend/api/streaming.py`) closes its DB `SessionLocal()` before returning the User object — necessary because Starlette WebSocket handlers don't compose with `Depends(get_db)` yield-style sessions — which leaves the User detached. Phase B.5's `_resolve_org_bucket` then called `getattr(user, "organizations", None)` on that detached User, which triggers a SQLAlchemy lazy-load and raises `DetachedInstanceError`. The exception propagated up the ASGI stack, the upgrade handshake never completed, and the client saw the worst-case `code=1006` close (abnormal). Every Connect attempt looped through `useReconnectingWebSocket`'s 500ms / 1s / 2s / 4s / 8s backoff and gave up. **Fix**: wrap the `getattr` in `try/except DetachedInstanceError`, falling back to the existing email-based bucket key. Added `DetachedInstanceError` to the `IndexError, TypeError, AttributeError` set the inner `try/except` already catches when walking the relationship. The B.5 per-org rate limit still works for eagerly-loaded callers (e.g. unit tests with a fresh session); the production WS path now degrades gracefully to per-user rate limit, which is a softer bound but still bounded. Backend rebuilt + redeployed; Connect succeeds, `<- {"type":"ready",...}` frame arrives, mic capture path is now reachable end-to-end. Smoke-tested on `meetingops.magicunicorn.dev` by Aaron immediately after deploy.

## [1.2.1] - 2026-05-26

### Added
- **Frontend speaker label badges in StreamingTest**: rendered the `speakers` array shipped on partial/final WS frames in v1.2.0. Each transcript entry now leads with one colored chip per distinct speaker that spoke during the window (sortformer's 4-speaker max → 4-color palette: red / green / blue / amber). Tooltip on each chip shows the speaker's first-turn start/end timestamps. Empty/undefined `speakers` is handled gracefully — entries without speaker data render identically to pre-v1.2.0. Removes the v1.2.0 deferral; the data was DevTools-observable but invisible in the UI, now it's visible inline. New types `SpeakerTurn` + helper `speakerColor(spk_id)` + presentational `<SpeakerBadges>` component in `frontend/src/pages/StreamingTest.tsx`. Docker frontend build (which enforces `tsc -b` strict per v1.1.0) succeeds; 5 baseline vite warnings unchanged.

## [1.2.0] - 2026-05-26

### Added
- **Phase B.3 chunk D: NVIDIA Sortformer streaming diarization end-to-end**: new `services/sortformer-svc/` container running `nvidia/diar_sortformer_4spk-v1` (123 M params, fp16) on midboy2 GPU 0. ~244 MiB resident / ~580 MiB peak during diarize. 175 ms warm round trip on a 33s window, RTF ~0.005 (~190x realtime). Co-resident with parakeet-svc + parakeet-stream-svc + speaker-svc on the same RTX 3060 with 6.5 GiB headroom; stress-tested 10-way concurrent (5 ASR + 5 diarize) without degradation. Service exposes `POST /diarize-stream` (per-window WAV/PCM body in, speakers JSON out with absolute timeline coords via `X-Window-Start-Ms`) and `POST /diarize-file` (full-meeting one-shot). Image layered on `meet-parakeet-svc:local` so we inherit the entire CUDA 12.4 + NeMo 2.4.1 + torch 2.4.1 stack; bigboy build → midboy2 ship via `docker save | docker load`. midboy2 compose adds `meet-sortformer-svc` service block on port 8896. 20 health tests pass. Why v1 not v2/v2.1: streaming variants need NeMo >= 2.5 and our parakeet base image pins 2.4.1; v1 still exposes `forward_streaming_step()` for the eventual true-streaming upgrade. See `docs/phase-b3-sortformer-spike.md` for the full spike report.
- **Phase B.3 chunk D: backend WS forwarder parallel dispatch**: `backend/api/streaming.py` `_flush_to_stt` now fires `asyncio.create_task` on the sortformer `/diarize-stream` POST BEFORE awaiting parakeet, when `STREAMING_USE_SORTFORMER=1`. Because sortformer is faster than parakeet (175 ms vs 200-600 ms warm), the second await is typically a no-op — true parallel inference, zero added latency. Sortformer failures (timeout / connect / 5xx / bad JSON) are non-blocking: empty `speakers` array on the outbound JSON frame, parakeet transcript still ships. Sortformer tasks are cancelled if parakeet errors out. Partial / final JSON frames gain four new keys (`speakers`, `sortformer_model`, `sortformer_rtf`, `sortformer_distinct_speakers`); existing clients ignore unknown keys. Production env flips `STREAMING_USE_SORTFORMER=1` in `.env.bigboy` and the backend service block in `deploy/bigboy/docker-compose.bigboy.yml` now forwards `STREAMING_USE_SORTFORMER` + `SORTFORMER_URL` + (newly explicit) `PARAKEET_STREAM_URL` into the container env. Live on `meetingops.magicunicorn.dev`.
- **Phase B.3 chunk B: Opus codec utility modules** (utility-only, no capture-path integration): `frontend/src/utils/opusEncoder.ts` (WebCodecs primary + MediaRecorder fallback, default 24 kbps speech, surfaces `createOpusEncoder({sampleRate, bitrate})` returning `{encode, flush, close}`) and `services/parakeet-stream-svc/opus_decoder.py` (libopus binding via `opuslib>=3.0.1,<4`, 20 ms frame default, 48 kHz internal). 12 pytest cases + 21 vitest cases pass. **Not yet wired** into `StreamingTest.tsx` capture path: the MediaRecorder fallback emits Opus-in-WebM (container) not raw Opus packets, so cross-browser integration needs either a WebM demuxer or a WebCodecs-only gate. Decision deferred to v1.3.0. Module reference compiles cleanly; if a future client wants to send Opus-coded audio, the encoder + decoder are available to call.
- **Phase B.3 chunk C: NeMo streaming `/transcribe-stream-v2` endpoint** (shipped as stepping-stone, NOT enabled in production): new endpoint in `services/parakeet-stream-svc/main.py` (now 970 lines, was 503) that uses NeMo's `conformer_stream_step` + `BatchedFrameASRTDT` to emit draft + finalize tokens with a session-stateful rolling audio ring. Spike report at `docs/phase-b3-nemo-streaming-spike.md` (268 lines) documents the architectural finding: `nvidia/parakeet-tdt-0.6b-v3` was trained with `att_context_size=[-1,-1]` (full-context attention), but cache-aware streaming requires `chunked_limited` attention. The streaming forward runs, but the model never learned to use the cache state, so it returns text only for the first chunk then nothing. **The fix is a checkpoint swap to `nvidia/multitalker-parakeet-streaming-0.6b-v1` (likely English-only) — multi-day eval follow-up.** v1 endpoint `/transcribe-stream` untouched and tested for back-compat (RTF=0.24, identical output). Endpoint gated behind `STREAMING_USE_V2_PARAKEET=0` (env default off); the integration scaffolding flag stays dormant until the model eval lands.
- **Phase B.3 integration scaffolding** (committed 2026-05-26 ahead of B.3 chunk landings): three new env vars in `backend/api/streaming.py` defaulting OFF so v1.0.0/v1.1.0 behaviour was unchanged at scaffolding time — `STREAMING_USE_V2_PARAKEET`, `STREAMING_USE_SORTFORMER`, `SORTFORMER_URL`. Design memo at `docs/phase-b3-integration-plan.md` (162 lines) documents the integration target spec for all four B.3 chunks. `STREAMING_USE_SORTFORMER` is now flipped to 1 in this release; the other two remain off.

### Changed
- **Phase B.3 chunk A: AudioWorklet migration for browser audio capture**: replaced the deprecated `ScriptProcessorNode` in `frontend/src/pages/StreamingTest.tsx` (the WS streaming smoke-test page) with `AudioWorkletNode`. ScriptProcessor's `onaudioprocess` runs on the main thread; AudioWorklet's `process()` runs in a dedicated audio render-thread, eliminating jank on long sessions and on devices with main-thread pressure (mobile, low-end laptops). New worklet processor at `frontend/public/audio-worklets/pcm16-encoder.worklet.js` (registered as `pcm16-encoder`, configurable frame size via MessagePort, default 3200 samples = 200 ms at 16 kHz). New helper at `frontend/src/utils/audioWorkletCapture.ts` wraps the AudioWorklet wiring behind a `startPcm16Capture(stream, onFrame, opts)` function; the helper transparently falls back to ScriptProcessorNode when `AudioContext.audioWorklet` is undefined (Safari < 14.1) with a console warning, so the page degrades rather than breaks on the rare legacy browser. The 19-byte big-endian WS frame header contract is preserved exactly — backend (`backend/api/streaming.py`) and `services/parakeet-stream-svc/` are unchanged. Bit-exact PCM16 conversion preserved (asymmetric `s < 0 ? s * 0x8000 : s * 0x7fff` mapping, matching the original ScriptProcessor loop). 5 baseline vite warnings unchanged. `tsc -b` clean. Verified production docker build of the frontend image succeeds with the worklet asset copied through to `dist/audio-worklets/`.

### Known scope deferrals
- **Frontend speaker label rendering**: backend now sends `speakers` array in partial / final WS frames, but `StreamingTest.tsx` does not yet render speaker badges next to transcript text. The data is observable in the WS log pane + browser DevTools network frames. UI polish queued for v1.3.0.
- **Opus uplink** (chunk B): utility modules shipped but capture path not wired (MediaRecorder=WebM caveat). v1.3.0 will either gate on WebCodecs-only or add a WebM demuxer.
- **True cache-aware streaming** (chunk C): blocked by checkpoint architecture per spike. Multi-day model eval cycle queued.

## [1.1.0] - 2026-05-26

### Added
- **Brigade Phase 2: inline 3D graph viewer**: new BrigadeGraphViewer component (lazy-loaded, react-force-graph-3d) embedded on SessionDetails. Shows the current meeting's Brigade nodes + 1-hop neighbors (Speaker, ActionItem, Topic, Decision) with color-coded labels and click-to-inspect tooltips. Backend: new GET /api/sessions/{id}/brigade-graph endpoint queries Brigade's Cypher endpoint and returns {nodes, links, graph_url} shape; returns empty + "not_synced_yet" reason for sessions before Brigade write fires. 30s cache. Cross-org isolated. "Open in Brigade" button below the viewer for the full-screen Brigade UI. The existing "View in Brigade graph" indigo banner from Phase 1 stays as an alternative entry point. The inline 3D viewer is gated behind a collapse/expand toggle so the Three.js footprint only loads when the user wants the graph. 7 new backend tests cover the endpoint shape, not-synced state, cross-org isolation, and cache.

- **Bulk Import B-import.4: admin pause/resume + Arq+Redis worker migration**: replaces the in-process asyncio queue with a durable Arq worker backed by unicorn-redis (db=4). Jobs now survive uvicorn restarts mid-batch. New compose service meet-bulk-import-worker running `arq backend.workers.bulk_import_worker.WorkerSettings`. Concurrency=2 default via Arq max_jobs; BULK_IMPORT_WORKERS env still respected. ARQ_ENABLED=false env flips back to the in-process pattern if needed. New admin endpoints: POST /api/import/admin/jobs/{id}/{pause,resume,cancel} + GET /api/import/admin/jobs (cross-org listing, admin-gated). Frontend AdminBulkImport page at /admin/bulk-import with status filter + per-job action buttons + click-through to file detail. 7 backend tests cover pause/resume/cross-org-visibility/non-admin-403/worker durability. The in-process queue stays in the codebase as ARQ_ENABLED=false fallback for emergency rollback.
### Fixed
- **TypeScript strict warnings cleanup**: `tsc -b` was emitting 17 strict-mode errors across the frontend even though `vite build` (which the Dockerfile uses) skipped tsc and shipped clean. Cleaned up the fixable ones across `frontend/src/components/PWAUpdate.tsx` (added `vite-plugin-pwa/client` type reference in `vite-env.d.ts` so `virtual:pwa-register` resolves), `frontend/src/components/TagChip.tsx` (replaced the dynamic-element `keyof JSX.IntrinsicElements` pattern with a conditional render — fixes JSX-namespace + IntrinsicAttributes errors), `frontend/src/components/ImportFilePickerStage.tsx` (removed always-true truthy check on `DataTransferItem.getAsFile` method reference), `frontend/src/contexts/AlwaysOnContext.tsx` (annotated `.map()` return type so the type predicate matches in `appendTranscriptSegments`; simplified the recovery-path `mimeType` resolution since the recorder ref is always null at that point), `frontend/src/pages/LiveRecording.tsx` (optional-chained `activeAgent` + `progressiveData` accesses; rewrote the nested-ternary `sessionData` narrowing that React 19 + TS 5.8 wasn't propagating through JSX), and `frontend/src/utils/api.ts` (coerced `?.startsWith()` result to a strict boolean). 17 strict errors -> 0. Vite production warnings unchanged at 5 (all upstream-library noise: 3 `"use client"` directives in react-toastify/react-router, 1 `eval` in onnxruntime-web, 1 chunk-size advisory). No behavior changes; pure type hygiene to keep CI output clean and unblock the strict `npm run build` script.
- **Pre-existing pytest failures cleared (21 -> 0)**: 21 long-standing failures across `tests/test_analytics.py`, `tests/test_search_analytics.py`, and `tests/test_parakeet_stt.py` had been carried since the security-hardening commit (ed9e70e) without being addressed. Root-caused and fixed in three buckets. (1) `tests/test_search_analytics.py` (15 fails) + the duplicate `tests/test_analytics.py` (4 fails): every test hit `/api/analytics/*` and `/api/simple/recording-sessions/*` without auth headers, which started 401-ing as of ed9e70e. Fixed by routing every call through `_auth_headers(client)` (login + bearer); deleted `test_analytics.py` outright since all four of its tests duplicated `test_search_analytics.py::TestAnalytics` with thinner asserts. (2) `tests/test_parakeet_stt.py::TestProviderRegistrySTTRouting` (2 fails): the three registry tests didn't depend on the `client` fixture so the session-scoped `app` fixture never ran, leaving SQLite without the `org_provider_settings` table. Added `app` as a method-level fixture param so table creation runs first. The `test_parakeet_resolves_when_provider_name_set` assertion also expected `meet-parakeet-svc:` endpoint but production sets `PARAKEET_SERVER_URL=http://meet-parakeet-svc:8881`; fixed by `monkeypatch.delenv("PARAKEET_SERVER_URL")` so the in-cluster default path is exercised. The `test_default_is_local_whisper` test predates the Phase B.2 flip of `STT_DEFAULT_PROVIDER` default to `parakeet`; pinned the env explicitly via `monkeypatch.setenv("STT_DEFAULT_PROVIDER", "local_whisper")` and added a sibling `test_default_is_parakeet_in_production` that pins the current default. (3) New interaction bug exposed by the cleanup: `tests/test_arq_worker.py::test_worker_respects_max_jobs_concurrency` called `importlib.reload(workers.bulk_import_worker)` which transitively re-imported `database.models`, putting fresh `Table` objects on `Base.metadata` and orphaning every FK relationship bound to the OLD tables. `test_bulk_import.py::test_duplicate_sha256_marked_skipped` (and any downstream test) then failed with `NoReferencedTableError`. Fixed by (a) rewriting the test to drive `services.bulk_import_queue.get_bulk_import_concurrency()` directly with `monkeypatch.setenv` (no reload needed), and (b) extending `tests/conftest.py::app` to reload `services.bulk_import_queue` + `workers.bulk_import_worker` after the model reload so their cached model refs rebind to the fresh `Base`. Final: `318 passed, 0 failed, 3 skipped` (previously `288 passed, 21 failed, 3 skipped`).
- **Docker frontend build now enforces TypeScript strict check**: `frontend/Dockerfile` used `npm run build:docker = vite build` which skipped tsc entirely — that's how 17 strict errors silently rode production for weeks AND how a runtime bug shipped in `frontend/src/pages/AdminBulkImport.tsx` (4 sites calling `showToast('msg', 'error')` as a function, but `showToast` is an object with `.success`/`.error`/`.info`/`.warning` methods — calls would no-op at runtime). Fixed by (a) rewriting `package.json::scripts.build:docker` to `tsc -b && vite build` (parity with `npm run build`), (b) cleaning up the AdminBulkImport.tsx call sites to use `.error(msg)` / `.success(msg)`, and (c) fixing the BrigadeGraphViewer.tsx ref typing for `react-force-graph-3d` (was casting to `MutableRefObject<unknown>`; lib expects the `ForceGraphMethods<...>` shape, cast to `any` since we don't use the imperative API). Docker build now fails fast on TS strict errors instead of shipping them.
- **Bulk-import worker healthcheck**: `meet-bulk-import-worker` shipped without a healthcheck, so when the B-import.4 worker started crash-looping due to the hardcoded Redis auth bug (since-resolved at f3857c0), it was only caught manually via `docker logs`. Added a Docker healthcheck (`docker-compose.bigboy.yml`) that pings the ARQ Redis instance the worker connects to via a Python one-liner; flips the container to unhealthy if Redis auth/DSN/connectivity breaks. 30s interval, 5s timeout, 3 retries, 30s start_period. Verified healthy after rebuild.
- **Dead code cleanup in `services/bulk_import_queue.py`**: removed 4 unused imports (`shutil`, `datetime.date`, `datetime.time`, `sqlalchemy.or_`) flagged by pyflakes. Also dropped the `_get_org_slug` helper + its caller (`org_slug = _get_org_slug(...)` at line 466 was computed but never read — the local-disk path the helper was originally meant to inform doesn't actually use it). Pure tidying; no behavior change.

## [1.0.0] - 2026-05-22

### Added
- **Bulk Import B-import.2: polished /import preview-table UX**: stage-based flow (Pick > Preview > Confirm > Progress > Completion). Folder drop-zone (webkitdirectory) accepts up to 1000 audio files at once with MIME validation; non-audio files rejected with toast. Preview table per-row controls: editable parsed title + meeting date + meeting time + participant hint + confidence indicator (green >=0.9, yellow >=0.5, red <0.5). Bulk-edit toolbar: apply participant to selected rows, set-all dates, deselect-low-confidence one-click. Confirm screen surfaces audio total + expected processing time + Garage bucket + GPU host. Progress stage shows per-file status with linked-session button on complete + per-failure retry button. Completion summary with "Retry failed" + "Go to sessions" deep-link. Backend: new POST /api/import/jobs/{id}/files/{file_id}/retry endpoint + ?bulk_import_job_id query filter on /api/simple/recording-sessions. Polish phase for Aaron's 526-file Mac Notes archive at /Volumes/media/audio-from-notes-voicememos-2026-05-20.
- **Brigade Phase 1.5: write from live/browser-only sessions**: extends the Phase 1 writer to also fire when a browser/live session is finalized via /sessions/{id}/finalize (not just the /finalize-audio reprocess path). write_meeting_to_brigade now gracefully handles missing speakers (no diarization in browser path), missing action items, missing topic/decision extraction; always writes at minimum a Meeting node + summary text. New completion_mode parameter ('reprocess' or 'live') stored on the Meeting node for analytics. Hook fires fire-and-forget at /finalize so user-facing flow is never affected. 5 new tests on top of the existing Phase 1 suite cover the live-completion path, the completion_mode tag, and failure swallowing.
- **Phase B.5: production polish on server-live streaming path**: closes Phase B for paid-tier launch.
  - Frontend `useReconnectingWebSocket` hook with exponential backoff (500ms -> 30s, max 5 attempts). StreamingTest page now uses it; clean closes (1000/1001/4xxx) don't reconnect, abnormal closes (1006) do. Reconnect status surfaced in the log pane.
  - Server-side backpressure in `backend/api/streaming.py`: per-session PCM buffer caps at 60s, oldest dropped if exceeded. Parakeet round-trip >2s triggers skip-next-N backoff. Per-org cap of 5 concurrent live sessions (configurable via `STREAMING_MAX_SESSIONS_PER_ORG`); excess gets close code 4429.
  - Prometheus metrics on `/metrics`: `meeting_ops_ws_connections_total` (labels tier + result), `meeting_ops_ws_audio_frames_forwarded_total`, `meeting_ops_ws_partial_transcripts_emitted_total`, `meeting_ops_ws_close_codes_total` (label code), `meeting_ops_parakeet_stream_request_duration_seconds` (histogram).
  - SIGTERM drain: active WS sessions get a `{"type":"server_shutdown","reconnect_after_ms":3000}` JSON frame + close code 1001 before uvicorn exits. Lets the frontend reconnect cleanly after a deploy.
  - 7 new backend tests cover backpressure, per-org rate limit, metrics increments, parakeet slowdown handling, active_sessions registry, org-count drain, and /metrics endpoint shape.
- Phase B is now production-polished end-to-end. Remaining blockers to paid-tier launch are pricing decision and upgrade flow (operational, not engineering).

## [0.9.1] - 2026-05-22

### Added
- **Bulk Import B-import.3: speaker auto-link from filename pattern**: when a bulk-imported file's parsed title matches the `Call with {Name}` pattern (which the bulk of Aaron's 526-file archive does), the pipeline now extracts the name via `extract_call_with_name()` in `backend/utils/filename_parser.py` (handles parenthetical suffixes like `Doug (Crash)` -> `Doug`) and resolves it via `find_speaker_by_name_hint()` in `backend/services/speaker_service.py` using rapidfuzz token_set_ratio at an 0.85 floor against enrolled speakers in the org. On match, creates a `SpeakerSessionLink` with `source='filename-hint'` and `raw_label='HINT'`. The existing post-reprocess `identify_speakers()` embedding-match (source='auto') runs later and adds its own link; both coexist. Cross-org isolation enforced in the lookup. New `backend/tests/test_bulk_import_speaker_link.py` covers happy path, fuzzy match (e.g., `Khan, Shafen` <-> `Shafen Khan`), parenthetical suffix stripping, cross-org isolation, unknown-name, non-call-pattern titles, and the Step 3.5 hook integration. 15 tests pass. Test harness gained a `RECORDINGS_DIR` env var (in conftest.py) pointing to a tempfile so bulk-import tests don't depend on the Docker-only `/app/recordings` path.
- **Phase B.4: tier gate on server-live WebSocket**: the `/ws/sessions/{session_id}/live` endpoint now enforces `tier_features.server_live` via the User.tier infrastructure shipped in v0.8.2 (alembic 028 + `backend/auth/tier.py`). Free-tier users attempting to connect get a JSON `{"type": "error", "reason": "tier_insufficient"}` frame followed by close code 4003. Pro / Enterprise users (and superusers via `get_user_tier()`'s is_superuser override) proceed to the existing ready / partial / final flow. Frontend `/streaming-test` page reads `useTierFeatures()` and renders an upgrade nudge instead of the connect button when `server_live` is false. 5 backend tests cover free / pro / enterprise / superuser-bypass / unauth-regression. Closes the gate that makes server-live a real paid feature; combined with the existing capture-only fallback for free mobile users in v0.8.0, free tier is now a coherent product.

## [0.9.0] - 2026-05-22

### Added
- **Phase B.2 — real Parakeet 0.6B v3 in meet-parakeet-stream-svc + WS forwarding**: the B.1 stub container is replaced with a real NeMo + CUDA streaming-ASR service. `services/parakeet-stream-svc/Dockerfile` now layers on top of the already-built `meet-parakeet-svc:local` image (saves rebuilding the CUDA 12.4 + NeMo 2.4.1 + torch 2.4.1 + audio-tooling stack from scratch — the new image is just a `pip install` and a couple of file copies). At container start, NeMo loads `nvidia/parakeet-tdt-0.6b-v3` into VRAM at fp16 with `nvidia/parakeet-tdt-0.6b-v2` as fallback; model load takes ~47 s the first time (HF download) and ~10-15 s on warm volumes. `POST /transcribe-stream` accepts a WAV body (or raw PCM16) and returns `{text, segments, words, duration, model, rtf, confidence, sequence, is_final}` with real transcripts. Verified on midboy2 GPU 0 (RTX 3060): model resident ~1.7 GB, total GPU 0 usage ~5.1 GB combined with the existing 1.1B batch + speaker-svc containers (~6.9 GB headroom). 2.5 s window inference at RTF 0.08-0.12 (10x realtime).
- **Phase B.2 — WS handler buffers + forwards to streaming service**: `backend/api/streaming.py` is upgraded from the B.1 ack-only scaffold to a real near-streaming forwarder. Each WS session keeps a 60 s ring buffer of PCM16, fires an upstream `POST /transcribe-stream` every 2.5 s of audio (or on `{"type":"flush"}`), and sends `{"type":"partial", text, sequence, covers_through_ms, ...}` JSON frames back to the client as transcripts arrive. The buffer is windowed to the most recent 25 s on each upstream call so it stays inside the streaming service's 30 s per-call cap regardless of how long the session has been running. `{"type":"end"}` triggers a synchronous final flush + `{"type":"final", ...}` frame before close-1000. Concurrency-safe: one upstream call in flight per session via `state.in_flight`. Connection-scoped `httpx.AsyncClient` with keep-alive limits avoids TCP setup on every flush. End-to-end test with the speaker-svc `synthetic_2speaker.wav` fixture (33.7 s, 2 Kokoro TTS speakers) returns 13 progressive partials + 1 final transcript that matches the ground-truth meta.json word-for-word ("…we need a stronger probe to catch it earlier in the future, before it impacts real user sessions"). True NeMo streaming with draft+finalize tokens is the B.3 enhancement; the wire contract is forward-compatible.
- **Phase B.2 — StreamingTest page with real-mic capture**: `/streaming-test` (admin) now has a Start mic button that captures the user's microphone via `getUserMedia` + `ScriptProcessorNode`, transcodes to 16 kHz mono PCM16, batches into 200 ms WS frames with the canonical 19-byte BE header, and streams to the backend. The page also has a "Live transcript" pane below the controls that aggregates partial / final JSON frames into a readable rolling transcript (green left-border for finals, blue for partials). Backwards-compatible — the Send dummy chunk button still works as a seam test, the 19-byte header layout is unchanged.
- **Phase B.2 — frame header reconciliation (doc-vs-code)**: the original `docs/phase-b-server-live-streaming.md` section 4 drafted a variable-length little-endian header (1-byte version + LE uint64 sequence + LE uint64 timestamp_ms + LE uint16 format + payload). B.1 shipped a fixed 19-byte big-endian header (`>IQ4sHB`: uint32 BE sequence + uint64 BE timestamp_us + 4-char ASCII format + uint16 BE sample_rate/100 + uint8 flags). B.2 reconciles to the **fixed BE form** because it carries identical information, parses with one `struct.unpack` on both Python and TypeScript sides, and avoids the read-version-then-branch overhead of the variable layout. Updated section 4 + Appendix A of the design doc, the streaming.py module docstring, and the StreamingTest comment block. Variable-LE form remains an option if we ever need extension fields, but it's over-engineered for v1.
- **Brigade integration Phase 1**: Meeting-Ops now writes session-completion data to Brigade FalkorDB as graph nodes (Meeting + Speaker + ActionItem + Topic + Decision + corresponding edges). Hooks into the existing post-reprocess pipeline so the write happens automatically after Parakeet 1.1B + pyannote + Qwen 3.6 finishes. Tenancy modes: shared / per_org_graph / per_org_instance via `BRIGADE_TENANCY_MODE` env (default `shared`, graph name `agent_meeting_ops_canonical`; per-org mode picks `agent_meeting_ops_org_<id>`). New `backend/services/brigade_client.py` (HTTP client with 3-attempt exponential backoff retry on connect/timeout/5xx, terminal 4xx, log-only no-op mode when `BRIGADE_API_KEY`/`BRIGADE_ADMIN_KEY` unset, `is_live` flag for callers) and `backend/services/brigade_writer.py` (orchestrates per-session write: Meeting node first, then Speaker nodes via SpeakerSessionLink lookup, then ActionItem nodes with best-effort owner→speaker matching for ASSIGNED_TO edges, then Topic nodes from `final_summary.bullets`, then Decision nodes from `final_summary.decisions` with parenthetical "decided by Name" parsing for DECIDED_BY edges; stamps `recording_sessions.brigade_synced_at` + `brigade_graph_node_id` on success). Brigade write failures NEVER break the user-facing pipeline (top-level try/except in writer + dedicated try/except in `_run_session_reprocess` Stage 6 hook; both log + continue). Frontend `SessionDetails` adds a "View in Brigade graph" link when `brigade_graph_url` is set on the session payload; backend pre-builds the URL via `services.brigade_writer.build_brigade_graph_url` so the client doesn't need to know the active tenancy mode. Alembic 030 adds `brigade_graph_node_id` (VARCHAR(64)) + `brigade_synced_at` (TIMESTAMP) columns to `recording_sessions` plus an index on `brigade_synced_at` for the future reconciliation job. End-to-end live test against Brigade v1.13.0 on bigboy verified entity + edge writes land in `agent_meeting_ops_canonical` graph. 7 backend tests cover writer fires after reprocess, full node+edge call shape (11 entities + 11 edges + 2 ASSIGNED_TO + 1 DECIDED_BY for the seeded session), shared-tenancy single-agent-id contract, per_org_graph isolation (disjoint agent_ids per org), failure-swallowing on Brigade errors, idempotent rerun (same node names + monotonic timestamp), and log-only mode when no API key. Phase 1 of `docs/brigade-integration-design.md`; Phase 2 (read endpoints + in-page 3D mini-widget) and Phase 3 (full `/insights` page with feature parity vs Brigade's KnowledgeGraph.jsx) are subsequent rollouts.

## [0.8.2] - 2026-05-22

### Documentation
- Added `docs/README.md` (58 lines) indexing all 14 design docs in `docs/` grouped by topic (architecture, mobile roadmap, bulk operations, conference rooms, ecosystem integration, deployment) with line counts + shipped/designed status. Includes a "reading order for new developers" section. Hand-maintained alongside design-doc commits.
- Updated `CLAUDE.md` with a Latest release line for v0.8.1, a Design docs section linking to `docs/README.md`, an Active roadmap section reflecting the mobile A -> B -> C-1 -> C-2 -> C-3 commitment.

### Added
- **Bulk audio import — Phase 1 (B-import.1)**: new `/import` page lets users upload multiple audio files at once, with the filename parser pre-extracting title + meeting_date + meeting_time. Backend job queue extends the existing `UploadPipelineQueue` pattern with a configurable concurrency cap (default 2 to protect Parakeet 1.1B reprocess load, override via `BULK_IMPORT_WORKERS` env, hard ceiling of 4). Per-file pipeline: SHA-256 dedup against existing sessions → filename parse → RecordingSession create → audio upload to Garage S3 (`meeting-ops-audio` bucket, `{org_id}/{job_id}/{file_id}` key) → internal reprocess enqueue. New `POST /api/import/jobs`, `POST /api/import/jobs/{id}/files` (multipart streaming, 8 MiB chunks, per-file size cap), `GET /api/import/jobs/{id}` (job + per-file rows + counters), `POST /api/import/jobs/{id}/cancel` (soft-cancel: queued files become skipped, in-flight finish gracefully). Alembic 029 adds `bulk_import_jobs` + `bulk_import_files` tables with FK cascades + `(user_id, status)` / `(job_id, status)` / `(file_sha256)` indexes. Org-scoped throughout; cross-org probes return 404 (no existence leak). 7 backend tests covering create-job, upload + parse, SHA-256 dedup, cross-org 404, concurrency-cap semaphore math, cancel-mid-flight, idempotent cancel. Targets Aaron's 526-file backlog at `/Volumes/media/audio-from-notes-voicememos-2026-05-20`. Polished preview-table UX, speaker auto-link from filename, admin pause/resume, and Arq+Redis migration are subsequent phases (B-import.2/3/4) per `docs/bulk-audio-import-design.md`.
- **Phase B.1 scaffolding (server-live streaming foundation)**: new `meet-parakeet-stream-svc` container on midboy2 (port 8895) with stub `/transcribe-stream` endpoint that returns dummy partial transcripts; real Parakeet 0.6B v3 integration lands in Phase B.2 per `docs/phase-b-server-live-streaming.md` sections 5-7. New `/ws/sessions/{session_id}/live` FastAPI WS endpoint in meet-backend (`backend/api/streaming.py`) implementing the 19-byte binary frame header + JSON control protocol (`>IQ4sHB`: uint32 BE sequence + uint64 BE client_timestamp_us + 4-char ASCII payload_format + uint16 BE sample_rate/100 + uint8 flags); auth-gated via the existing oauth2-proxy forward-auth header flow (same path the production `/ws/audio-levels` endpoint uses); accepts connection, emits a `ready` frame, logs each binary chunk + acks it, replies `pong` to `{"type":"ping"}`, closes cleanly on `{"type":"end"}`, returns close-code 4001 for unauthenticated connects. Admin-only `/streaming-test` frontend page (`frontend/src/pages/StreamingTest.tsx`) lets you Connect, Send dummy chunk (1 KB PCM16-formatted), Send ping, Send end, and watches the round-trip with full frame log. End-to-end seam (Cloudflare -> oauth2-proxy -> Traefik -> FastAPI WS upgrade) verified.
- **User tier infrastructure**: new `users.tier` column (alembic 028) with values free / pro / enterprise. Aaron + Shafen backfilled to `enterprise` + `is_superuser=true`. New `backend/auth/tier.py` module with `TIER_FEATURES` dict, `get_user_tier()`, `get_tier_features()`, and `require_tier(min_tier)` FastAPI dependency. `/api/auth/me` response now includes `tier` and `tier_features` fields (the latter is a nested dict with per-capability booleans + quota numbers). Frontend `useTierFeatures()` hook + TS types added. Foundation for Phase B.4 server-live tier gating.

## [0.8.1] - 2026-05-22


### Changed
- Phase B design doc Q1-Q4 open questions locked (`e6d85f2`): Q1 new `meet-parakeet-stream-svc` container running Parakeet 0.6B v3 on midboy2 GPU 0 (existing 1.1B batch on bigboy stays for post-meeting reprocess); Q2 new users default to `tier=free`, Aaron + Shafen set to `tier=enterprise` + `is_superuser=true` via alembic UPDATE on their email rows (tier and admin orthogonal); Q3 Traefik sticky routing for B.1-B.4, defer Redis pub/sub until we add a second backend replica (existing `/ws/audio-levels` proves the single-worker pattern works); Q4 no new seam to test for oauth2-proxy WS through Cloudflare, the existing `/ws/audio-levels` already routes the exact same way in production.
- TS cleanup: settings components and SettingsEnhanced.tsx now use `import type` for `SectionProps` / `SaveStatus` / `HostCapabilities` / `SettingsState` (`ca3ca01` via Codex). The types existed and were exported; the warnings were value-level imports of type-only exports under `isolatedModules`. Vite production build is now warning-free.

### Documentation
- Added `docs/phase-c1-native-ios-design.md` (~1386 lines) scoping the native iOS app with Core ML + watchOS extension. Stack: Swift + SwiftUI native (because watchOS apps must be Swift extensions of iOS bundles). Core ML conversion: use FluidInference/FluidAudio Swift Package (MIT) which already ships Parakeet 0.6B v3 as `.mlpackage` (110x RTF on M4 Pro, INT8 W8A8 ANE-optimized); LLM via runtime priority cascade (Apple Foundation Models 3B on iOS 26+ via Swift API, fallback to mlx-swift Qwen 3 0.6B / Gemma 4 E2B in 4-bit on iOS 17+, fallback to llama.cpp + Metal, fallback to capture-only). Native UX (AVAudioEngine + URLSession background uploads, MPRemoteCommandCenter lock-screen, INVoiceShortcut Siri, CoreSpotlight indexing, share-extension, widgets, Dynamic Island Live Activities), GRDB local DB, WS auth via Sec-WebSocket-Protocol bearer JWT (no cookie). watchOS extension via WatchConnectivity proxying through paired iPhone (B.1) + cellular direct upload (B.2). 5-phase plan (~8-10 weeks for v1 closed beta + 2-4 weeks App Store review). Phase C-1 in the committed mobile roadmap A->B->C-1->C-2->C-3.
- Added `docs/bulk-audio-import-design.md` (~1054 lines) covering the /import page for bulk audio ingestion: UX flow (drop folder → preview table with parsed metadata + bulk-edit overrides → confirm → progress view → completion), job queue architecture (recommendation: in-process asyncio queue for v1 with Arq + Redis migration in v4, vs Celery rejected as sync-first), per-file pipeline (SHA-256 dedup → filename parse → session create → Garage write → reprocess enqueue), concurrency model (=2 default with per-org rate limit + adaptive throttle when Parakeet p95 climbs above 180s), progress UI with WebSocket primary + SSE/polling fallbacks, error handling + restart recovery + resume-after-cancel semantics, speaker auto-link from `Call with X` filename pattern (new `source='filename-hint'` value, embedding match wins), Garage S3 storage layout (`meeting-ops-audio` bucket, `{org_id}/{session_id}/{filename}` key format), security (per-file/per-job/per-org-per-day caps + MIME validation via ffprobe + filename sanitization), alembic 028 schema for `bulk_import_jobs` + `bulk_import_files` tables, and 4-phase implementation plan (~5 working days). Direct continuation of today's filename parser + meeting_date column work (`e447cf9`), targeting Aaron's 526-file backlog at `/Volumes/media/audio-from-notes-voicememos-2026-05-20`.

### Added
- **Synthetic-WAV diarization probe for speaker-svc**: new `GET /healthz/synthetic` endpoint runs a bundled 2-speaker test WAV through the full pyannote + wespeaker pipeline at each probe and asserts `speaker_count` matches ground truth (2). Returns 503 with `reason` on `speaker_count_mismatch` / `segments_too_few` / `embedding_distance_too_low` / `diarization_threw` / `fixture_missing` / `fixture_unreadable`, 200 otherwise. Docker healthcheck wired to call it every 5 minutes with `start_period: 2m` for cold-start tolerance; midboy1 runtime compose updated to match. Catches the pipeline-state-degradation class of failure that the standard `/health` misses; the threshold-sweep false alarm a few days ago was caused by exactly this (pipeline degraded but threshold was fine, container restart fixed it). Refactored shared diarization path into `_run_diarization_on_wav()` so the probe hits the exact same code path normal sessions hit. Fixture is a 33.7s 16 kHz mono WAV concatenating two Kokoro TTS speakers (`af_bella` female + `am_michael` male, ~1MB) with a sibling `synthetic_2speaker.meta.json` describing ground truth + the verification baseline (observed `num_speakers=2`, observed centroid cosine distance 0.977 against the 0.4 floor). 6 unit tests cover fixture-present / ok / speaker_count_mismatch / diarization_threw / embedding_distance_too_low / segments_too_few. Closes #105.
- **Editable meeting_date + meeting_time columns on recording sessions**: backfilled from started_at / created_at via alembic 027. Inline-editable on SessionDetails with native date + time inputs. Session list now supports sort-by-meeting-date in addition to sort-by-upload-date (persisted in localStorage as a "Uploaded / Meeting date" toggle in the controls bar). Backend list endpoint accepts `?sort=meeting_date_desc|meeting_date_asc|created_at_desc`; null meeting_date falls back to started_at then created_at so a session never disappears from the date column.
- **Filename parser (backend + frontend)**: shared utility that extracts title / date / time / source from filenames. Recognizes the `notes__/downloads__` Mac Notes + Voice Memos export pattern (`YYYY-MM-DD_HHMMSS` prefix, confidence 1.0), plus generic ISO-date-prefix (0.8-0.9), US-date-suffix (0.7), and ISO-date-anywhere (0.5) fallbacks. Used at single-file upload time to pre-fill the new-session form (preview card with confidence + accept/override checkbox); also runs server-side at upload finalize so any user-supplied meeting_date / meeting_time wins over the filename guess. Foundation for the upcoming `/import` bulk-upload page that will backfill Aaron's 526-file audio archive at `/Volumes/media/audio-from-notes-voicememos-2026-05-20`.
- New `POST /api/recordings/parse-filename` endpoint exposing the parser to clients (user-scoped auth; returns `title` / `meeting_date` / `meeting_time` / `source` / `confidence` / `raw_filename`).
- **Privacy mode full-quality local pass (Phase A.6)**: at session stop in privacy mode, the browser now runs Parakeet 0.6B INT8 against the assembled IndexedDB audio blob for a high-quality full transcript, then runs Qwen 3 0.6B INT8 (or Gemma 4 E2B) for the final summary. Previous privacy summaries were live-slice rollups only; now they match server-quality privacy-respecting output. Results stored in new local-sessions IndexedDB store.
- **Local Sessions view**: new /local-sessions page lists privacy-mode sessions persisted in IndexedDB. Sidebar entry appears once you have at least one local session. Detail view supports audio playback (from IDB), inline title/tag edits, and export-as-Markdown / export-as-JSON for moving data out of the browser. Local-only badge on every card. Truly zero-server-bytes architecture across capture, STT, summary, storage, and review.

## [0.8.0] - 2026-05-21

### Added
- **Mobile capture-only mode**: phones and tablets now skip browser-side Parakeet 0.6B STT and Qwen 3 0.6B / Gemma 4 E2B LLM loading entirely. New `frontend/src/utils/deviceDetection.ts` runs one-shot probe (UA + WebGPU + maxTouchPoints for iPadOS disambiguation + SharedArrayBuffer + deviceMemory) and buckets devices into `desktop-capable` / `capture-only` / `desktop-fallback`. `AlwaysOnContext` reads the result at provider mount, gates the Parakeet load effect and `triggerSummarize` browser-LLM path on `shouldRunBrowserInference`, exposes `deviceCapability` + `isCaptureOnlyMode` for the UI. `AlwaysOnControl` swaps the live transcript + live summary panes for a "Capture-only mode" banner on mobile; `MobileLiveRecording` shows the same banner above its scroll area. Audio capture continues via MediaRecorder and uploads as chunks through the always-on path; the server runs the full quality pass (Parakeet 1.1B + pyannote 3.1 + Qwen 3.6 35B) at completion and produces transcript + summary about a minute later. This eliminates the "live captions stall on iOS Safari WebGPU" experience and sets honest expectations. First step in the committed mobile roadmap (Phase A → B → C-1 → C-2 → C-3, where C-2 includes Apple Watch support).
- **Desktop local-first audio persistence (Phase A.5)**: chunks now land in IndexedDB during recording in addition to streaming to the server. New `frontend/src/services/localAudioStore.ts` keyed by `[session_id, sequence_number]` plus a session-metadata store for orphan tracking. On stop, the client computes a SHA-256 + byte total + chunk count across the IDB mirror and POSTs a verification payload to `/finalize-audio`; server reassembles the on-disk chunks, recomputes the SHA-256, and either returns `status=complete` (client wipes local) or `status=incomplete` with `missing_chunks` + `server_chunks` (client falls back to whole-file upload). New `/full-audio` endpoint accepts the assembled IDB blob as a bulletproof recovery path — clears any partial chunk state and queues the same reprocess pipeline. Gated to `capabilityClass='desktop-capable'`; mobile and desktop-fallback stay chunks-straight-to-server.
- **Resume-on-reload banner**: orphan sessions (recordings that started but never finalized) are detected at provider mount via a one-shot IDB scan. `AlwaysOnControl` renders a banner per orphan with started-at relative time, chunk count, MB size, and Resume Upload / Discard actions. Resume runs the same verify-then-fall-back-to-full-audio dance; sessions older than 24 hours prompt for confirmation. A browser crash mid-meeting now loses at most the last partial chunk in the encoder buffer instead of the entire recording.
- **Privacy mode is now truly local**: `localOnly: true` on the recorder skips `/audio-chunks` POSTs entirely while still persisting each chunk to IndexedDB so the local STT (Parakeet 0.6B INT8) and local LLM summary (Qwen 3 0.6B / Gemma 4 E2B) pipelines have source audio. No `/finalize-audio` either. The session ends with `localAudioStore.setSessionFinalized()` (audio + sha256 stay on the device) and the existing local-session store keeps transcript + summary + slices. Local STT/LLM pass against the assembled blob is queued as a follow-up — today the slice rollup at stop() produces the final summary from the live-transcribed slices, same as pre-A.5 privacy mode.
- New Help page subsection "On mobile (phones and tablets)" explaining the capture-only contract, why current mobile browsers can't do the live work reliably, and the roadmap to native iOS+Android with on-device live captions sized for phone silicon.
- meeting-ops.magicunicorn.dev (hyphenated) as alias for canonical meetingops.magicunicorn.dev. Cloudflare CNAME (proxied) + Traefik 308 redirect via redirectregex middleware. Cookies/sessions stay on canonical; URL works either way.
- **Browser always-on full audio capture + server-side reprocess**: parallel `MediaRecorder` track on the same MediaStream as the VAD engine produces continuous WebM/Opus (or MP4 on Safari) chunks every 30s; each chunk POSTs to new `/api/recordings/sessions/{id}/audio-chunks` with exponential-backoff retries (max 5). On session end, `/finalize-audio` reassembles via ffmpeg and kicks a BackgroundTasks pipeline: Parakeet 1.1B fp16 → pyannote diarization (`return_embeddings=true`) → `identify_speakers()` → Qwen 3.6 35B-A3B-Vision final summary. `processing_metadata.reprocess_status` drives a banner on SessionDetails (in_progress → complete). Privacy/local-only mode skips audio upload entirely — nothing leaves the device.
  - 2 new HTTP cross-org leak cases (audio-chunks POST + finalize-audio POST → 404 across orgs).
  - Dedicated unit/HTTP test file `test_audio_chunks_reprocess.py` for the wire contract (5 tests).

### Changed
- `frontend/src/utils/fullAudioRecorder.ts`: new `localPersistence` + `localOnly` options. When `localPersistence` is on, each MediaRecorder `dataavailable` blob is persisted to IDB before being queued for upload (the persistence step lands first so a tab crash mid-network-request preserves the chunk). When `localOnly` is on, the server upload path is completely skipped. New `postFinalizeAudioWithVerification()` and `postFullAudio()` helpers handle the verification dance + whole-file fallback. Recorder handle gains `localBytes()` + `mimeType()` accessors so callers can label the assembled blob and surface a "buffered locally" counter.
- `backend/api/recording.py`: `/finalize-audio` accepts an optional Pydantic `FinalizeAudioVerification` body (`client_chunk_count`, `client_bytes_total`, `client_sha256`). When present, server recomputes the SHA-256 from the chunk files on disk (async via `asyncio.to_thread` so a 60MB session doesn't block the event loop) and compares to the client claim. Match returns `status=complete` and queues the reprocess; mismatch returns `status=incomplete` with the chunk-index delta and does NOT queue the reprocess. Pre-A.5 finalize-audio calls without a body still work — the verification is opt-in.
- New `/api/recordings/sessions/{id}/full-audio` endpoint (dual-auth like `/audio-chunks`) accepts a single multipart `audio` field, clears any existing chunk state for the session, writes the recovery blob as a zero-padded "000000" chunk so ffmpeg's concat step still sees it, and queues the same `_run_session_reprocess` pipeline `/finalize-audio` runs. Returns `status=complete` + `bytes_received` + `source: full_audio_fallback`.
- `frontend/src/contexts/AlwaysOnContext.tsx`: `start()` initialises the local-audio store metadata row on desktop-capable browsers and passes `localPersistence: true` (plus `localOnly` matching privacy mode) to the recorder. `stop()` runs the verify-then-fallback dance for standard sessions and finalizes the IDB session for privacy sessions. New context surface: `orphanSessions`, `resumingOrphan`, `resumeOrphanSession`, `discardOrphanSession`. `discardSession()` also wipes the local IDB mirror.
- `frontend/src/components/AlwaysOnControl.tsx`: orphan-session banner above the recording controls — only renders when state=idle and there are local sessions still pending verification.
- `frontend/src/components/MobileLiveRecording.tsx` no longer holds the recording in memory and uploads as one blob at Stop. It now spins up the same `fullAudioRecorder` desktop has used since v0.7.4: parallel MediaRecorder running at a 30s timeslice, sequential FIFO upload queue with exponential-backoff retries (max 5), idempotent on `chunk_index`. On Stop the recorder flushes its current encoder buffer, drains the queue, then the component POSTs `/finalize` followed by `/finalize-audio` to kick the server-side reprocess (Parakeet 1.1B fp16 + pyannote + Qwen 3.6 35B-A3B-Vision). A crashed Safari mid-meeting now loses at most the last 30 seconds; before this change it lost the entire recording.
- **iOS Safari MIME path verified MP4-first**: `frontend/src/utils/fullAudioRecorder.ts` `pickRecordableMime` now branches on a WebKit-family probe (UA-sniff that also catches iOS Chrome/Firefox/Edge, all of which use WKWebView) and probes `audio/mp4;codecs=mp4a.40.2` → `audio/mp4` → `audio/aac` before falling back to `audio/webm`/Opus. This pins production iOS sessions on the years-stable MP4/AAC path even on Safari 18.4+ which newly advertises WebM/Opus support. Server-side ffmpeg reassembly takes either; the choice is purely about which MediaRecorder code path is best-tested on iPhone hardware right now.
- **Mobile recorder polish**: Screen Wake Lock acquired while recording (Safari 18.4+ PWAs honor this; older versions silently no-op so we never throw); visibilitychange surfaces a "tab is in the background" amber banner because iOS can suspend a hidden MediaRecorder and we want the user to know rather than silently losing audio; a live upload-queue indicator shows `N uploaded, M queued, K dropped` so flaky network is visible instead of silent; a "Done. Server is processing." confirmation card with a deep link to Sessions appears after Stop instead of dumping the user back to idle.
- **Mobile recorder dropped the `useUploads` integration**: the `/api/uploads/start` one-shot path is replaced by `/api/recordings/start-always-on` + `/audio-chunks` + `/finalize` + `/finalize-audio`. Mobile sessions are now `mode=always_on` rows in the recording_sessions table (same as desktop browser always-on) and pick up the same server reprocess banner on SessionDetails. No backend changes.

### Fixed
- **Auth: fall back to user's first org instead of platform default** (`e186779`) — users with org memberships outside `magic-unicorn` (the platform default org slug) were getting 403s on every endpoint requiring `get_current_organization`, because `resolve_active_organization` would default to `ensure_default_organization` and then fail the membership check. Hit GFL + shafen-khan org users hardest. Fix: when `current_user.organizations` is non-empty, pick `current_user.organizations[0].organization` as the implicit active org; only call `ensure_default_organization` for users with literally zero memberships (the auto-provision case). Frontend can still override via X-Organization header or ?org= query param.
- **Live summary repeats early content**: `services/summary_slices.maybe_auto_trigger_slice` previously gated on `session.room_id` so only conference-room sessions got server-rolled slices; browser always-on used Qwen 3 0.6B INT8 in the browser, which couldn't sustain incremental summarization with previous-summary context. Gate removed — browser always-on sessions now share the same Qwen 3.6 35B-A3B-Vision server path with `triggered_by='auto-words'` (rooms keep `triggered_by='room-recorder'`). Frontend `AlwaysOnContext.triggerSummarize` now POSTs the manual slice + polls `GET /summary-slices` every 5s for standard sessions; privacy/local-only mode keeps the browser Qwen 3 0.6B path as the only option that respects the "nothing leaves the device" contract.

### Documentation
- Added `docs/compute-economics.md` (~290 lines) covering the browser-first architecture, unit-economics moat (10-100x lower per-user cost vs Otter / Granola / Fathom / Fireflies / Read.ai), competitor cost comparison with named services (AssemblyAI / Deepgram / OpenAI), proposed Free/Pro/Enterprise tier model (pricing TBD), deployment topology (magicunicorn.dev → unicorncommander.ai → enterprise), watch-outs (mobile, live browser quality, bandwidth), and the guiding principle for future architecture decisions (maximum browser-side compute, server only where quality genuinely requires it, third-party APIs only as a last resort).
- Updated `README.md` with a top-of-file "Architecture: browser-first by design" section: text diagram of browser ↔ server completion flow, the moat framing, the proposed tier shape, and the deployment progression. Links to the full strategy doc.
- Updated `CLAUDE.md` with the browser-first architecture principle near the project overview so future engineers default to browser-side compute when adding features.
- Added in-app Help page at `/help` (`frontend/src/pages/Help.tsx`) with a user-facing explainer: "Your computer does the real-time work" / "Our servers run the quality pass when your meeting ends" / "Privacy mode keeps everything local" / "Why we built it this way." Wired into the sidebar nav and routing.
- Added `docs/phase-b-server-live-streaming.md` (~1400 lines) covering server-live streaming as a paid-tier feature: WebSocket protocol (binary PCM16 framing + JSON control + sequence-replay reconnect), backend Parakeet 0.6B v3 streaming + pyannote-deferred (with Sortformer streaming as the B.3 upgrade path) + Qwen 3.6 35B-A3B rolling summary every 5s/200 words, frontend WS client with AudioWorklet PCM transcoding + 3-attempt exponential backoff + capture-only fallback, tier gating via new `User.tier` field + `tier_features` block on `/api/auth/me`, cost modeling vs Otter / Granola / Fathom / Fireflies (their $0.50-0.80/concurrent-user-hour vs our ~$0.01 on already-paid-for hardware, ~20 concurrent Pro users on current GPU footprint), <500ms first-word latency budget, failure-mode matrix (WS drops / GPU saturation / uvicorn restart / tier revoked mid-session / mid-session org reassignment), privacy-mode mutual-exclusion semantics, and a 5-phase implementation plan (~10-12 days: B.1 WS+Parakeet, B.2 rolling summary, B.3 frontend, B.4 tier gate, B.5 reconnect+backpressure+telemetry+drain). Phase B is the next chunk after the v0.8.0 Phase A bundle (mobile capture-only + desktop local-first + privacy mode end-to-end). Targeted at Pro / Enterprise users on mobile and weak desktops.

## [0.7.4] - 2026-05-21

### Changed
- **LLM consolidation**: all 6 Meeting-Ops LLM consumers now use Qwen 3.6 35B-A3B-Vision on midboy1 P40 (dedicated llama-server, -np 4 --cont-batching, 98K context, vision-capable) — same production endpoint Listing-Ops uses. Previous default gemma-4-26b-moe on bigboy 3090 retired. Frees bigboy 3090 for Parakeet/diarization/embedding workloads.
- Meeting Assistant DB row migrated from provider_type=ollama to provider_type=litellm (model_name unchanged).
- Default model environment variables (LLM_MODEL_QUALITY, LLM_MODEL_CHAT) in docker-compose.bigboy.yml and .env.bigboy* now point to Qwen 3.6.

### Added
- backend/scripts/qwen36_consolidation_bench.py: reproducible benchmark script for future model comparisons.
- Alembic migration 026_qwen36_consolidation (idempotent + reversible).

### Fixed
- Thinking-mode handling: documented that /no_think in user prompt is echoed back literally by Qwen 3.6 on llama.cpp nothink.jinja template. Correct control is chat_template_kwargs.enable_thinking: false. Memory note updated.

### Performance
- Final summary on session 103 transcript: 38.2s → 9.0s (4x faster, also more accurate).
- TTFB latency: 0.74s → 0.25s (3x faster).
- Throughput: 14.8 tok/s → 31.9 tok/s.

## [0.7.3] - 2026-05-21

### Added
- **Session attachments**: file uploads attached to session records (Granola notes, external transcripts, supporting docs, etc.).
  - Alembic 025 session_attachments table with FK cascades.
  - Garage S3 storage (bucket meeting-ops-attachments) + local-disk fallback.
  - Multipart upload, list, download, edit, delete endpoints.
  - Frontend drop-zone, list with metadata editing, paperclip icon + count on Sessions list.
- **Move session between orgs**: POST /move-org endpoint with cascade (transcripts, slices, action items, collaborators, attachments, Qdrant re-tag, audit log). Speaker-link orphan surfacing in response. Frontend modal with org dropdown.
- Design docs: appliance build extraction (~530 lines), image registry decision, Brigade integration (1332 lines).

### Changed
- **Forgejo canonical**: git.unicorncommander.ai is source of truth; git.magicunicorn.dev is backup mirror.

### Fixed
- Stale "Granite 3.3 8B" copy in Meeting Assistant description (both unified_agent.py default and live DB row).
- Clustering threshold investigation found root cause was pipeline-state degradation (not threshold value) — kept at 0.700. Filed follow-up for synthetic-WAV correctness probe.

### Removed
- Dead frontend/src/SessionDetails.tsx (root-level orphan; live route is pages/SessionDetails.tsx).

## [0.7.2] - 2026-05-21

### Fixed
- **Speaker auto-match**: embeddings now preserved end-to-end through always-on completion, file upload, and chunked recording paths. Confirmed with 0.83 similarity on test audio. /rediarize endpoint can backfill historical sessions.
- **Markdown export**: regex now accepts md/markdown, ExportOptions.includeTranscript latent crash patched, per-user filter replaced with org-scoped _get_session_for_export. PDF + DOCX 501 stubs also fixed.
- **Email modal**: renamed "Share via email", added free-form Other recipients chip section, magic-link tokens scoped to single session (cross-org safety test passing).

### Added
- Postgres backup taken pre-feature: /srv/backups/meet_db/meet_db_2026-05-21_pre-v072.sql.gz.

## [0.7.1] - 2026-05-20

### Added
- **Internal service token** for room_recorder → /chunks loopback POST (X-Internal-Service-Token, secrets.compare_digest, fail-closed on empty env).
- **Server-side auto-summary slice persistence** for room sessions: RecordingSession.processing_metadata.summary_slices JSONB, bounded 200/session.
- GET/POST /api/recordings/sessions/{id}/summary-slices endpoints.
- WebSocket broadcast of new slices to all room viewers.
- Per-device secret on satellite_devices (bcrypt-hashed, generated at pairing redemption).
- Alembic 024 satellite_device_secret migration.
- DeviceSecretReveal frontend component (UI wiring deferred until hardware exists).

### Security
- Satellite WebSocket /ws/satellite/{device_id}/audio now requires Authorization: Bearer device secret.
- 5 satellite HTTP endpoints gain dual-auth (device OR user).
- In-memory leaky-bucket rate limiter on failed satellite auth (5 fails / 10 min → 30-min lockout).

### Changed
- midboy2 speaker-svc autoheal + healthcheck (~8h restart cadence) prevents VRAM bloat.
- Long-term pyannote leak fix recommendations documented.

## [0.7.0] - 2026-05-20

### Added
- **Conference Room recording mode (Phase 1)**: new /rooms list + 5-step setup wizard + 3-tab detail page.
- backend/api/rooms.py with CRUD, lifecycle, pairing codes (6-digit, 10-min TTL).
- services/room_recorder.py per-room arecord subprocess + WebRTC VAD + chunk POSTs.
- Alembic 023 conference_rooms migration (rooms, audio_sources, pairing_codes, ACL).
- Multi-room concurrent native, chunk-based (no SRT/RTP/Icecast).
- Server-multi-mic as Phase 1 primary deployment.
- Live VU meters (SSE), live transcript + auto-summary panes.
- /dev/snd + audio group passed into meet-backend (USB mics now visible).

### Changed
- **UX overhaul around user intent, not mechanism**: three-card use-case picker on /record/personal (In-person / Online meeting / Mixed). On-the-fly source switching mid-recording.
- Admin section in sidebar with ShieldCheck divider.
- Settings regrouped: Your preferences / Workspace setup / Advanced.
- AdminRoute wrapper protects admin URLs.
- Alembic-first entrypoint (create_all removed — no more migration race).
- Page-flash eliminated across Rooms / Sessions / Dashboard / RoomDetail / SpeakerLibrary via localStorage hydration + matched skeletons.
- midboy2 Parakeet fp16 default pinned in compose.

### Fixed
- **Satellite API cross-org leak (CR-001)**: every endpoint now filters by organization_id.
- Diarization Off toggle plumbed through chunk pipeline.
- webrtcvad-wheels swap (setuptools 82 compatible).
- isAdminRole helper centralized in utils/roles.ts.

### Security
- Cross-org leak test extended to 29 parametrized cases (all passing).

## [0.6.0] - 2026-05-19

### Added
- **Granola-style slice-stack live summary** with auto-trigger thresholds.
- **transformers.js runtime backend** for in-browser LLM (Qwen 3 / Gemma 4 / SmolLM2).
- **Parakeet 0.6B INT8 browser STT**: audio never leaves the device when enabled.
- **Local-only privacy mode**: transcript + summary in IndexedDB, no server sync.
- **Mic UX overhaul**: device picker, USB auto-prefer, hot-swap, VU meter, mic test.
- Qwen 3 0.6B as in-browser default with structured prompt + thinking-mode off.

### Changed
- Parakeet weights served from HuggingFace directly (940 MB image bloat removed).

### Removed
- Whisper retired: STT default now Parakeet (1.95 GB VRAM freed on RTX 6000).
- Dead meet_chunks pipeline deleted (closed cross-org leak risk).
- Orphan MeetingChunkEmbedding table dropped.

## [0.5.0] and prior

See git tag annotations directly: git tag -l v0.5.0 -n100.

## [0.0.1] - 2026-02-26

### Added
- Initial versioned release.

[unreleased]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/compare/v1.0.0...HEAD
[1.0.0]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v1.0.0
[0.9.1]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.9.1
[0.9.0]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.9.0
[0.8.2]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.8.2
[0.8.1]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.8.1
[0.8.0]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.8.0
[0.7.4]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.7.4
[0.7.3]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.7.3
[0.7.2]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.7.2
[0.7.1]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.7.1
[0.7.0]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.7.0
[0.6.0]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.6.0
[0.0.1]: https://git.unicorncommander.ai/UnicornCommander/UC-Meeting-Ops/releases/tag/v0.0.1
