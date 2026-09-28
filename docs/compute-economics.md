# Compute Economics

How UC-Meeting-Ops achieves 10-100x lower per-user cost than competitors by running live work in the browser and only touching server GPUs at meeting completion.

> Status: design + strategy doc. Architecture is live as of v0.7.4 (`80b2da1`). Tier model in this doc is proposed; pricing decisions are pending.

## TL;DR

Every other meeting-AI product on the market (Otter, Granola, Fathom, Fireflies, Read.ai) pays a third-party API per audio-minute and per token, per user, continuously, for the entire duration of every meeting. That cost scales linearly with concurrent users.

Meeting-Ops doesn't. We run live transcription and live summarization in the user's browser via WebGPU + WASM, using small INT8/quantized models cached in IndexedDB. The user's own CPU/GPU does the per-minute work. Then, only when the meeting ends, we run a single 30-90 second server pass for the high-quality transcript, diarization, and final summary. Our marginal cost per concurrent user-hour is zero. Our marginal cost per completed meeting is ~30-90 GPU-seconds amortized across idle ecosystem capacity. This is a real moat. Replicating it requires rebuilding the entire pipeline from scratch with a model selection, browser runtime, and quality-budget choice that most competitors will not make.

The same architecture also gives us a free privacy mode (toggle the server step off, audio never leaves the device), an Enterprise/HIPAA on-prem path that doesn't require running our cloud GPUs at the customer, and graceful degradation on weak devices. But privacy is the side benefit. The primary intent is unit economics.

---

## The architecture

Two stacks doing two different jobs.

### Browser stack (live work)

Runs in the user's tab for the entire duration of the meeting. Everything below is downloaded once per device, cached in IndexedDB, and re-served from cache on every subsequent meeting.

| Component | Model | Runtime | Job |
|-----------|-------|---------|-----|
| Live STT | Parakeet-TDT 0.6B INT8 | onnxruntime-web (WASM, WebGPU when available) | Streaming transcript |
| Live summary | Qwen 3 0.6B (default) or Gemma 4 E2B | transformers.js / web-llm | Rolling slice-stack summary every ~500 words |
| VAD + chunking | Silero VAD via @ricky0123/vad-web | WASM | Speech segmentation |
| Audio capture | MediaRecorder (WebM/Opus, MP4 on Safari) | Native browser API | Per-30s chunks for later server pass |

Everything in this row runs on the user's device. The server sees nothing in real time except small chunk uploads (audio bytes only, for the server pass at the end). There is no per-token cost to us during the meeting. There is no per-audio-minute cost to a third-party STT vendor. The user's tab does the work because the user's tab can.

Parakeet 0.6B INT8 hits ~0.13x real-time on an M2 in WASM (measured) and roughly matches a small Whisper for English. Qwen 3 0.6B with structured prompts and `enable_thinking=false` runs incremental summarization fast enough that the summary stays under the user's word-counter. Gemma 4 E2B is the fallback runtime for devices where Qwen 3 0.6B is awkward; both are wired and switchable in In-browser AI settings.

### Server stack (completion work)

Runs exactly once per completed meeting, when the user stops recording (or the always-on engine segments a meeting on silence). Takes 30-90 seconds depending on meeting length and queue depth.

| Component | Model | Where | Job |
|-----------|-------|-------|-----|
| Full STT | Parakeet 1.1B fp16 | midboy2 RTX 3060 | High-quality transcript with word timestamps |
| Diarization | pyannote 3.1 | bigboy 3090 / RTX 6000 | Speaker turn detection |
| Speaker embeddings | wespeaker (ECAPA-TDN) | bigboy 3090 / RTX 6000 | Auto-match against speaker library |
| Final summary | Qwen 3.6 35B-A3B-Vision | midboy1 P40 (np 4 cont-batching) | Polished summary, action items, attendee tracking |

The wire contract is in `backend/api/recording_audio_chunks.py` + `services/audio_chunks_finalize.py`. Browser POSTs WebM/Opus chunks to `/api/recordings/sessions/{id}/audio-chunks` while recording (retried with exponential backoff up to 5 times). On stop, the browser calls `/finalize-audio`, which reassembles the chunks with ffmpeg and enqueues a BackgroundTask running the four-stage pipeline above. `processing_metadata.reprocess_status` drives a banner on the session-details page (`in_progress` → `complete`).

### Privacy mode

The browser stack runs identically. The server stack does not run. Audio bytes never leave the device. `processing_metadata.reprocess_status` is set to `disabled`. Output is browser-only: Parakeet 0.6B transcript and Qwen 3 0.6B summary, both persisted to IndexedDB and to the server transcript record (text, not audio).

Privacy mode is a per-session toggle in the recorder. It is orthogonal to pricing tier. Anyone can enable it. Enterprise and HIPAA deployments default it on.

---

## The economics

Concrete numbers, not vibes. The point is to make the moat legible.

### Our cost

**Per concurrent user-hour during a meeting**: ~$0 in compute. The browser does the work. We pay for the ~28 MB/hour of audio chunks the browser uploads (bandwidth), but bandwidth at this volume is well below a cent on any reasonable cloud or our own bare metal.

**Per completed meeting**: ~30-90 GPU-seconds. Parakeet 1.1B on a 3060 runs faster than real-time, pyannote + wespeaker on a 3090 is ~10-15 seconds for a typical hour-long meeting, Qwen 3.6 35B-A3B at np=4 on a P40 generates a final summary in 5-15 seconds. The exact wall-clock depends on meeting length, but it's bounded and runs on idle ecosystem capacity. Even if we wanted to put a number on it: a P40 at $0.30/hour cloud-equivalent and 60 seconds of GPU time is $0.005 per completed meeting on the LLM side. Add comparable amounts for STT and diarization and you're under $0.02 per completed meeting all-in, at retail GPU prices we don't pay.

**At 1000 concurrent users in meetings, simultaneously**: $0/hour in compute while the meetings run, plus ~1000 × $0.02 = ~$20 in GPU time when they all finish, smeared across whenever each meeting actually ends.

### Competitor cost

Otter, Granola, Fathom, Fireflies, Read.ai, and the rest are paying:

- **STT** at AssemblyAI ($0.37/hr base, $0.65/hr with diarization) or Deepgram (Nova-3 ~$0.15/hr base, ~$0.43/hr with diarization + sentiment) or self-hosted Whisper on cloud GPUs (still real per-minute compute).
- **Live LLM summarization** at OpenAI/Anthropic ($3-15 per million input tokens, $15-75 per million output tokens). A 60-minute meeting with rolling summaries every 500 words is 5-15K input tokens per summary refresh × 6-12 refreshes per meeting + final summary. Conservative: $0.10-0.30 in tokens per meeting just for the live side.

A typical competitor pays $0.50-1.50 per hour-long meeting per user, every meeting. Their per-user-hour cost is roughly proportional to active meeting time.

### The delta

At 1000 concurrent users in hour-long meetings:

| | Their cost | Our cost |
|---|---|---|
| Compute (during meeting) | $500-1500/hr | ~$0 |
| Compute (completion) | (included above) | ~$20 (one-time per meeting) |
| Bandwidth | small | ~$0.50 (1000 × 28 MB) |
| Total | $500-1500/hr | ~$20/hr equivalent |

That's 25-75x. At larger scale and longer meetings, the gap widens, because their cost scales linearly with meeting-minutes and ours scales linearly with completed meetings (with a flat ~30-90 sec each, regardless of meeting length).

### Why it stays a moat

This isn't a price war we win by being cheap. It's a structural difference in where the compute happens. Three things have to be true for a competitor to replicate it:

1. **They have to ship browser inference.** Most don't. Their entire product is built around a server-side STT vendor and an LLM API. Ripping that out is a multi-quarter rebuild.
2. **They have to find browser-runnable models that don't embarrass them.** Parakeet 0.6B INT8 and Qwen 3 0.6B aren't obvious choices today. We've validated them; competitors would have to do the same model selection + quality budget work, plus pick a browser runtime (web-llm vs transformers.js vs raw onnxruntime-web) that actually performs on the devices their users actually have.
3. **They have to accept that live quality is "good enough for live" instead of "best possible for live."** This is a product decision their existing positioning may not allow. If their pitch is "AssemblyAI-grade transcripts in real time," they can't downgrade live without rewriting their marketing too.

We made all three decisions early, validated each, and the stack is in production. The architecture is the moat. Pricing is just the visible consequence.

---

## Privacy mode as a free side benefit

The browser-first architecture means privacy mode costs us nothing to offer. The server step is conditional; turning it off is a feature flag.

In privacy mode:

- Audio chunks are never uploaded.
- Transcript is computed locally via Parakeet 0.6B INT8 and stored to IndexedDB.
- Summary is computed locally via Qwen 3 0.6B and stored to IndexedDB.
- Server still stores the *text* transcript + summary for cross-meeting search, RAG, and sharing — but no audio bytes leave the device.
- For full local-only mode (no server text either), pair privacy mode with the local-only session flag.

The tier model below treats privacy as orthogonal: Free, Pro, and Enterprise all offer it. Enterprise and HIPAA deployments default it on. The HIPAA path documented in `docs/brigade-integration-design.md` (Legacy1 medical-practice instance) inherits the same toggle — same code, same architecture, just with the server reprocess and Brigade graph push disabled by default and gated on per-patient consent.

It is genuinely the same architecture. We didn't bolt privacy on; the architecture was already this shape for cost reasons, and privacy fell out.

---

## Proposed pricing tier model

Implementation TBD. Documenting the intent so the tier shape is in the open. Pricing decisions are still pending and this section should not be treated as launched product copy.

### Free

- Browser live transcript (Parakeet 0.6B INT8)
- Browser live summary (Qwen 3 0.6B)
- Local search across their own meetings (in-browser)
- No server reprocess. Final transcript = browser transcript. No speaker diarization, no Qwen 3.6 summary, no auto-match against a speaker library.
- Privacy mode available.

The Free tier is genuinely good. It is not a crippled demo. For a solo user who just wants their meetings captured, it works.

### Pro

- Everything in Free, plus
- Server reprocess on meeting completion (Parakeet 1.1B fp16 + pyannote 3.1 + wespeaker)
- Qwen 3.6 35B-A3B-Vision final summary, action items, attendee tracking
- Cross-meeting RAG against high-quality transcripts (rather than browser-only transcripts)
- Speaker library + speaker auto-match
- Brigade graph push (Meeting/Speaker/ActionItem/Topic/Decision nodes)
- Privacy mode still available (per-session toggle)

### Enterprise

- Everything in Pro, plus
- Org-wide speaker library
- Retention controls (auto-delete after N days, audit trail)
- BYOK Brigade (per-tenant FalkorDB graph: shared, per-org-graph, or per-org-instance)
- HIPAA mode (privacy mode defaults on, audit logging, BAA)
- Dedicated server or on-prem appliance
- SSO via Keycloak (`uchub` realm federation)

Enterprise on-prem packaging depends on the appliance extraction work (`docs/appliance-extraction-design.md`).

---

## Deployment topology

The progression for tier rollout. Each environment has a specific job.

```
meetingops.magicunicorn.dev          unicorncommander.ai             Enterprise (dedicated)
meeting-ops.magicunicorn.dev   →     (production)                →   on-prem / HIPAA
(dev + beta tier)                    (Free + Pro, stable)             (Legacy1 medical, etc.)

v0.x lives here today.               Pinned to stable releases.       Per-tenant instance,
Paid beta possible.                  Free + Pro launch here.          BYOK Brigade graph,
Where we ship features first.        Aaron-stated 2026-05-21.         appliance-extracted.
```

### magicunicorn.dev (where we are today)

`meetingops.magicunicorn.dev` is canonical, `meeting-ops.magicunicorn.dev` is the hyphenated alias (Cloudflare CNAME proxied + Traefik 308 redirect to canonical). Cookies and sessions stay on the canonical hostname; the URL works either way.

This is the dev + beta tier. v0.x ships here. New features land here first. We may run a paid beta on this domain before unicorncommander.ai cuts over.

### unicorncommander.ai (production target)

Stable releases only. Free + Pro tiers launch here. Aaron's directive (2026-05-21) is that the production domain stays clean of half-baked features. Promotion is `v0.x` → tag → ship to unicorncommander.ai once we're happy with what landed on the dev tier.

### Enterprise

Dedicated server per customer, possibly on-prem appliance. HIPAA deployments (Legacy1, etc.) get their own instance with privacy mode defaulted on, the Brigade graph tenancy mode set per customer requirement (shared / per_org_graph / per_org_instance per the integration design), and BAA where required.

The appliance build (the Ryzen AI 780M + Vulkan box from the early architecture) extracts cleanly from the cloud build per `docs/appliance-extraction-design.md` and is the same codebase with different compose targets.

---

## Watch-outs and risk factors

The architecture works in production. It also has soft spots that need active attention.

### Live browser quality must stay good

If the user perceives live transcription as bad, the whole product feels bad even though the server-side completion output is excellent. The mental model people apply to meeting AI is "what I see while it's recording is what I'm getting" — they don't read release notes about a server pass that fixes everything in 30 seconds. The live experience has to be good enough that they don't notice the upgrade at completion, and the completion upgrade has to be visibly higher quality (better punctuation, real speaker names, polished summary) so the wait feels worth it.

Mitigations:

- Live STT defaults to Parakeet 0.6B INT8, which is good enough on most M-series and modern x86 laptops.
- Live summary defaults to Qwen 3 0.6B with structured prompts. We've validated Qwen 3 0.6B over Gemma 4 E2B as the default; Gemma is available as a fallback for specific devices.
- We pre-fetch + cache model weights aggressively. First-load is the only painful experience.
- We surface load progress so users don't think the recorder is hung.

### Mobile is the soft spot

iOS Safari WebGPU is slow and gated behind feature flags on older iOS versions. Android WebGPU varies wildly across vendor/chipset combos. WASM works everywhere but is slower.

Mitigations:

- Graceful degradation from WebGPU → WASM is wired in onnxruntime-web and transformers.js.
- Privacy mode also runs in WASM — no regression there.
- A future "server-live" tier (paid upgrade for weak devices) would push live STT + live summary to our servers as a per-user paid option. We haven't built it; we've designed for the option.
- Mobile recording on the PWA (`MobileLiveRecording.tsx`) intentionally simplifies the live UI to reduce the perception gap on slow devices.

### Bandwidth

Each user uploads about 28 MB of audio per hour of meeting (WebM/Opus at typical bitrate). At scale on paid tiers, this is real but small money. The bigger concern is mobile users on cellular — chunked upload with backoff handles intermittent connectivity, but the user should be warned.

Mitigations:

- Chunk size is configurable. Today it's 30s per chunk; smaller chunks = better resilience, larger chunks = lower overhead.
- Privacy mode obviously eliminates this entirely.
- We could (and may, on Free tier) downsample audio before upload to halve the bandwidth.

### Third-party API fallback temptation

When something feels slow or hard locally, the natural temptation is "just call OpenAI for this one thing." Resist. Every time we add a third-party API call we're giving back the moat. If a feature genuinely requires it, document why and put it behind a feature flag that defaults off.

The right framing for every new feature: "can we do this in the browser? If not, can we do it server-side on our GPUs at meeting completion? Only if both are no, consider an external API." See the principle section at the end of this doc.

---

## Implementation status

Where each piece stands as of 2026-05-21.

| Component | Status | Since |
|-----------|--------|-------|
| Browser STT (Parakeet 0.6B INT8) | Live | v0.6.0 (2026-05-19) |
| Browser LLM (Qwen 3 0.6B default, Gemma 4 E2B fallback) | Live | v0.6.0 (Qwen default since Task #59) |
| In-browser AI settings panel | Live | v0.6.0 |
| Privacy mode (no server sync) | Live | v0.6.0 (Task #62) |
| Audio chunk upload (browser → server) | Live | v0.7.4 + 1 (`80b2da1`) |
| Server reprocess pipeline (Parakeet 1.1B + pyannote + Qwen 3.6) | Live | v0.7.4 + 1 (`80b2da1`) |
| Speaker auto-match end-to-end | Live | v0.7.2 |
| Server-rolled live summary (Qwen 3.6) | Live | v0.7.4 + 1 — replaces browser Qwen 3 0.6B for standard sessions |
| Tier gating (Free / Pro / Enterprise) | Pending — pricing decision pending | — |
| unicorncommander.ai production deployment | Pending | — |
| Enterprise on-prem packaging | Pending — depends on `docs/appliance-extraction-design.md` | — |
| HIPAA mode (Legacy1) | Pending — Brigade integration design landed; deployment TBD | `docs/brigade-integration-design.md` |
| Mobile graceful-degradation polish | Pending | — |

The architecture is in production. What's pending is the commercial wrapper around it: pricing, the production domain cutover, and the Enterprise/on-prem packaging.

---

## How to think about future architecture decisions

The guiding principle for every new feature, every new model, every new pipeline stage:

> **Maximum browser-side compute. Add server compute only where quality genuinely requires it. Add third-party APIs only as a last resort, behind a flag, with the cost owner explicit.**

Concretely, when someone proposes a feature, walk through this in order:

1. **Can the user's browser do it?** If yes, do it there. The CPU cycle is free to us. The user doesn't notice. The privacy story stays clean.
2. **Does it require the post-meeting completion pass?** If yes, fold it into the existing pipeline. The pipeline runs once per meeting, on idle GPU, batched. Adding a stage doesn't change the unit economics.
3. **Does it require a separate server-side service that runs during the meeting?** This is the dangerous case. It breaks the per-user-hour zero-cost property. Only acceptable if (a) the feature genuinely requires it and (b) it's a paid-tier feature.
4. **Does it require a third-party API?** Only as a last resort. Document why. Put it behind a flag. Default it off in the Free tier. Make sure the cost owner is explicit (us, the customer's BYOK, etc.).

Cross-cutting reminders:

- **Don't make the browser stack worse to make the server stack easier.** The live experience is what users perceive. Server quality is icing.
- **Cache aggressively.** Model weights, embeddings, transcripts — once a thing exists, don't recompute it. IndexedDB on the client, Qdrant + Postgres on the server.
- **Don't reach for OpenAI or Anthropic by default.** We have local models that are good enough for almost everything. Use them. The whole point of the architecture is to not be on the meter.
- **Default new features to private/per-user scoping.** Sharing is opt-in. Org-wide is admin. Cross-org is forbidden. See cross-org leak tests (29 parametrized cases passing as of v0.7.0).
- **Test the cross-org boundary.** Every new endpoint that touches session data needs an org leak test before merge.

If a future feature can't honor the browser-first principle, that's fine — but it has to be a deliberate decision with a written rationale, not a default reach for an external API.

---

## Related docs

- `docs/always-on-recording-design.md` — the live-recording pipeline (browser side)
- `docs/appliance-extraction-design.md` — extracting the on-prem build from the cloud build
- `docs/brigade-integration-design.md` — graph integration + tenancy modes (incl. HIPAA path)
- `docs/conference-room-design.md` — room-level recording (also routes through completion pipeline)
- `docs/satellite-devices-design.md` — ESP32/RPi remote capture (also routes through completion pipeline)
- `CHANGELOG.md` — per-release notes on what shipped when
- `CLAUDE.md` — the canonical pointer for new engineers

If you change anything substantive about the browser/server split, update this doc and link the change in `CHANGELOG.md` under the relevant release.
