# Browser-side AI models

What runs in the user's browser, how much it weighs, where it's cached, and what to do when it breaks.

> Status: live. Reflects the shipped browser stack in `frontend/src/services/inBrowserSTT.ts`, `frontend/src/services/inBrowserLLM.ts`, and the device gating in `frontend/src/utils/deviceDetection.ts`.

This doc is the operational companion to [`compute-economics.md`](compute-economics.md). That one explains *why* the browser does the live work. This one explains *what* the browser actually downloads, how it's cached, what gets gated to capture-only mode, and how to recover when a first-run download fails.

---

## What runs in your browser

The live transcript and live rolling summary you see on the Record page are produced by two models the browser fetches on first use and caches forever after. Nothing in this list talks to a third-party API.

| Model | Job | Runtime | Cached on first use |
|-------|-----|---------|---------------------|
| Parakeet-TDT 0.6B INT8 | Live STT (audio → text) | onnxruntime-web 1.26 (WebGPU when available, WASM fallback) | CacheStorage (`meetops-parakeet-0.6b-int8-hf-v1`) |
| Qwen 3 0.6B ONNX q4f16 | Live rolling summary (default) | transformers.js (WebGPU) | IndexedDB (transformers.js cache) |
| Gemma 4 E2B q4f16 | Live rolling summary (premium quality, text-only path of multimodal repo) | transformers.js (WebGPU) | IndexedDB (transformers.js cache) |

The transcript and summary are wired so the user sees them appear *before* any chunk reaches a server. The chunks that go up are for the meeting-end completion pass; they don't drive what's on screen during the meeting.

### Parakeet 0.6B INT8 — STT

NVIDIA Parakeet-TDT 0.6B v3, INT8-quantized for browser execution. Source repo `huggingface.co/nasedkinpv/parakeet-tdt-0.6b-v3-onnx-int8`. Four files are fetched on first session and cached in CacheStorage:

| File | Size | Notes |
|------|------|-------|
| `encoder-int8.onnx` | ~1.4 MB | Graph |
| `encoder-int8.onnx.data` | ~838 MB | External weights (the bulk of the download) |
| `decoder_joint-int8.onnx` | ~52 MB | LSTM + joint network |
| `vocab.txt` | ~92 KB | SentencePiece vocab + blank |
| **Total** | **~890 MB** | One-time, cached forever |

Validated at ~0.13× real-time factor on M2 Mac in WASM during the spike. WebGPU is tried first; ORT silently falls back per-op to WASM for INT8 ops not yet covered on the WebGPU EP.

### Qwen 3 0.6B ONNX q4f16 — default live summary

`onnx-community/Qwen3-0.6B-ONNX` via transformers.js. **~570 MB** cached in IndexedDB. Context window 32k. Thinking mode disabled at the template level (`enable_thinking: false`) and via the `/no_think` tail tag — Qwen 3 ignores its `<think>` block, other model templates ignore the tag. Structured prompt captures ~90% of facts; output is grouped Decisions / Action Items / Metrics / Risks.

### Gemma 4 E2B q4f16 — premium live summary

`onnx-community/gemma-4-E2B-it-ONNX` via transformers.js. The repo is multimodal (text + vision + audio encoders) but we load it text-only via `AutoModelForCausalLM` — transformers.js skips the vision/audio encoder shards entirely when that loader resolves a causal-LM config. Full multimodal download is ~3 GB; the text-only path lands at roughly half that. Context window 8k. Better summary quality than Qwen 3 0.6B at the cost of slower cold-start and more memory pressure.

### Server fallback

A "Server-side Qwen 3.6" pseudo-entry sits in the picker for users whose browser can't (or won't) load the in-browser LLM. It routes slice summaries to `/api/recordings/summarize-slice` over SSE. STT has no server-side live equivalent in this code path — live STT is browser-only at the free tier. Paid tiers get a separate server-live streaming pipeline (see "What goes to the server" below).

---

## Device classes

`frontend/src/utils/deviceDetection.ts` buckets every device into one of three classes at provider mount and gates the model loads on the result.

| Class | What this matches | Live STT | Live summary | Notes |
|-------|-------------------|----------|--------------|-------|
| `capture-only` | iPhone, iPad (legacy UA + modern Mac-UA with `maxTouchPoints > 1`), Android | Skipped | Skipped | Audio capture + upload only. Server completion pass produces transcript + summary after stop. |
| `desktop-capable` | Desktop with WebGPU exposed | Parakeet 0.6B INT8 (WebGPU → WASM per op) | Gemma 4 E2B if user picks it; Qwen 3 0.6B by default | Best path. |
| `desktop-fallback` | Desktop without WebGPU (Firefox today, older Safari) | Parakeet 0.6B INT8 (WASM only, single-threaded if no SharedArrayBuffer) | Qwen 3 0.6B in WASM via transformers.js, slow but works | Banner-free; we don't block the path on missing WebGPU. |

Mobile gets routed to capture-only because:

- iOS Safari WebGPU pre-falls back to WASM at sub-1× real-time on these models.
- Mobile origin-storage caps evict 1 GB+ of cached weights aggressively.
- Background-tab JS throttling kills capture mid-meeting on every mobile browser.

The user still gets full transcript + summary — they're produced by the server completion pass when the meeting stops. No quality loss, just no live captions on the phone screen.

iPadOS detection: modern iPads send a desktop Mac user-agent. We disambiguate with `navigator.maxTouchPoints > 1` (Macs cap at 0 or 1; iPads report 5+) combined with a Mac-like `navigator.platform`, to avoid false-positives on touchscreen Chromebooks.

---

## First-run download

The first time a user opens `/record` on a fresh device:

| Tier | Files downloaded | Total |
|------|------------------|-------|
| Free / Pro / Enterprise on desktop | Parakeet 0.6B INT8 (4 files) + Qwen 3 0.6B (default) | ~1.46 GB |
| Same, after upgrading the picker to Gemma 4 E2B | Parakeet + Gemma 4 E2B text-only | ~2.3-3 GB depending on whether the fallback multimodal loader engages |
| Mobile (capture-only) | Nothing | 0 |

This is a **one-time** cost per device per model. Subsequent sessions hit the cache and start instantly. The cache survives:

- Page reloads.
- Browser restarts.
- Closing and reopening the tab.
- Logout / re-login (cache is keyed by URL, not by session).

The cache is invalidated when:

- The user clears site data manually.
- The browser evicts under storage pressure (rare on desktop with multi-GB quotas; common on mobile, which is one of the reasons we don't try mobile).
- We bump the cache key in source (`CACHE_NAME = 'meetops-parakeet-0.6b-int8-hf-v1'` in `inBrowserSTT.ts`). Bumping the key forces a one-time re-download for every user; we only do it when we ship a different INT8 export.

The Parakeet weights are fetched from HuggingFace directly. HF 302-redirects large files to a `cas-bridge.xethub.hf.co` CDN; `fetch()` follows redirects transparently. CORS is reflected per-Origin so no proxy is needed. A `MIRROR_BASE` fallback constant is in place (currently disabled) and ships a Backblaze B2 mirror behind a one-line switch if HF rate-limiting becomes a real problem.

Qwen 3 and Gemma 4 are fetched by transformers.js, which manages its own IndexedDB-backed cache and progress events.

---

## What if download fails

The most common failure modes, in rough order of frequency:

1. **Network interrupted mid-download.** Encoder weights are 838 MB — a flaky connection during the initial Record open is the usual cause. The fetcher does not currently resume from partial bytes; a failed `fetch()` will throw, the load aborts, and the next `/record` open retries from scratch (with the cache empty, since we only `cache.put()` on successful read).
2. **IndexedDB / CacheStorage quota exhausted.** Browsers evict origin caches when total disk fills. Common on small SSDs running multiple PWAs. The fetch succeeds but the `cache.put()` fails silently (we suppress) and the next reload re-downloads. Symptom: every cold open re-downloads the 890 MB.
3. **User cleared site data.** Same effect as quota exhaustion — next open re-downloads everything.
4. **HuggingFace rate-limit (HTTP 429).** HF allows ~3000 resolves per 5 minutes per IP. Behind a shared NAT (corporate office), enough concurrent users opening Record at once can trip it. The fetcher throws and the user sees the load fail; the fix is to retry a minute later, or for ops to flip `MIRROR_BASE` to the B2 mirror.
5. **CORS blocked by a corporate proxy.** Rare, but some MITM proxies strip the CORS reflection. Indistinguishable from a network failure at the JS level.

Recovery, from the user's side:

- **Retry by navigating away from `/record` and back.** A fresh load() attempt starts from the cache (any successfully cached files are reused) and only re-fetches what's missing.
- **Clear site data and retry.** Settings → Site settings → Clear data for the Meeting-Ops origin. This wipes the partial cache and starts clean. Fixes case 2 and case 3.
- **Use the Server-side picker.** In Settings → In-browser AI, switch the summarizer model to "Server-side Qwen 3.6". Bypasses the in-browser LLM entirely; live summary streams from `/api/recordings/summarize-slice`. STT is still browser-side, so the user still needs the Parakeet download to succeed for live captions.

Recovery, from the ops side:

- Watch for `[STT] HF fetch failed` warnings in client error logs (the fetcher logs them before throwing).
- If HF 429s are a recurring problem, set `MIRROR_BASE` in `inBrowserSTT.ts` to a B2 mirror URL and ship. The fetcher already tries the mirror on HF failure when set.

The server-side completion pipeline is independent of the browser models. **A user whose browser can't load Parakeet still gets a full transcript and summary at meeting end**, as long as their tier includes server completion (Pro and above). Free-tier users without a working browser STT see no live captions and no server completion — the recording still saves to local IndexedDB, but it's the only output they get.

---

## What goes to the server, what stays local

Per tier:

### Free

Nothing leaves the device. Live STT runs in the browser via Parakeet 0.6B. Live summary runs in the browser via Qwen 3 0.6B. Audio is held in IndexedDB locally. The browser-produced transcript text is the only artifact persisted to the server transcript record (text, not audio). Server completion is disabled on the tier.

### Pro

Audio chunks (WebM/Opus, ~30 s each) POST to `/api/recordings/sessions/{id}/audio-chunks` while the meeting is recording. The chunks are queued on the server but **not processed live** — they sit until the user stops the recording. On stop, the browser calls `/finalize-audio`, which reassembles the chunks and runs the 30-90 second completion pipeline (Parakeet 1.1B fp16 + pyannote 3.1 + wespeaker embeddings + Qwen 3.6 35B-A3B-Vision). Output: high-quality transcript, speaker diarization with library matching, polished summary.

The browser-side transcript and summary are still produced live during the meeting on Pro. The server-side artifacts replace them when the completion pass finishes.

### Enterprise / Founding 100

Same as Pro for completion. Additionally exposes the server-live streaming path (`docs/phase-b-server-live-streaming.md`): Parakeet streaming-trained 120M for per-word emit, optional Sortformer 4-speaker for live diarization, EOU detection. This runs concurrently with the browser-side transcript — the Record page shows both panes side-by-side. Gated on `hasFeature('server_live')`.

### Privacy mode (any paid tier)

Per-session toggle in the recorder (lock icon). When on, the browser stack runs identically but the server stack is skipped entirely. No chunks are uploaded. On stop, the browser runs a full-audio Parakeet transcription + a Qwen 3 / Gemma 4 final-summary pass over the recording that was held in IndexedDB during the session. Output is browser-only and persisted to the server as text (not audio), with `processing_metadata.reprocess_status = 'disabled'`.

Privacy mode requires WebGPU (the final-summary roll-up needs it; the live slice path tolerates WASM but the final pass is GPU-only). `isPrivacyModeAvailable()` in `frontend/src/services/privacyMode.ts` gates the toggle.

HIPAA / clinical / regulated deployments default Privacy mode on at the org level. End-users can leave it on for every session; the per-session toggle still works for ad-hoc opt-outs.

---

## See also

- [`compute-economics.md`](compute-economics.md) — the strategy doc this is the operational complement to.
- [`phase-b-server-live-streaming.md`](phase-b-server-live-streaming.md) — the paid-tier server-live streaming pipeline that runs *alongside* the browser models.
- [`phase-c1-native-ios-design.md`](phase-c1-native-ios-design.md) — the native-iOS plan that replaces capture-only mode on iPhone / iPad with on-device Core ML inference.
- `frontend/src/services/inBrowserSTT.ts` — the STT loader.
- `frontend/src/services/inBrowserLLM.ts` — the LLM loader + model catalog.
- `frontend/src/utils/deviceDetection.ts` — the device-class buckets.
- `frontend/src/services/privacyMode.ts` — the privacy toggle.
