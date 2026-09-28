# Local-only (Privacy) mode — pipeline & device gates

*Written 2026-07-17 (v3.57.x). Owner surface: `frontend/src/contexts/AlwaysOnContext.tsx`,
`frontend/src/services/inBrowserSTT.ts`, `frontend/src/utils/deviceDetection.ts`,
`frontend/src/services/localAudioStore.ts`.*

Local-only mode is the per-session toggle where **audio bytes never leave the device**. The
entire pipeline — capture, live transcription, live summaries, and the stop-time
full-quality pass — runs in the browser. Nothing is uploaded; the session's source of truth
is IndexedDB (`uc-meeting-ops-recordings`), surfaced in the Local Sessions UI.

## The two-stage stop flow

When the user stops a local-only recording:

**Stage 1 — live-slice roll-up (always runs).** The live summary slices produced during the
meeting are rolled into a final summary by the on-device LLM. If the meeting was too short to
ever produce a slice (< ~500 words), the raw live transcript is summarized directly instead
(≥ 30 words required) — so even a 2-minute local meeting ends with a real summary. This
stage is cheap and is the guaranteed floor: whatever happens later, the session closes with
*something* useful.

**Stage 2 — full-quality chunked pass (gated).** The whole recording is re-transcribed with
Parakeet-TDT 0.6B INT8 and re-summarized. Since v3.57.0 this runs **chunked** via
`inBrowserSTT.transcribeLong()`:

- Audio is decoded **once** to 16 kHz mono PCM (~3.8 MB per minute of audio held in memory).
- The PCM is processed in **~5-minute windows**, each cut at the quietest 100 ms frame in the
  15 s before the boundary (an inter-sentence pause) so words are never sliced in half.
- The mel-spectrogram loop yields a macrotask every ~20 s of audio, and there is a macrotask
  gap between windows — the page stays responsive for the entire pass.
- Progress is surfaced live: `Transcribing locally — 34 of ~90 min (38%)`.
- Each window sits inside Parakeet's ~24-minute long-form envelope, so long-meeting
  transcripts are **more** accurate than the pre-3.57 single-shot pass (which fed the whole
  meeting through one mel loop + one encoder run — the thing that froze a tester's MacBook
  on 2026-07-16).

If Stage 2 fails or is skipped, the Stage 1 summary is kept and the session row records why
(`markParakeetPassFailed` → visible in the Local Sessions UI).

## Device gates (`shouldRunFullLocalPass`)

| Device class | Stage 2 policy |
|---|---|
| `capture-only` (phone/tablet without WebGPU) | Never (no browser inference at all) |
| `desktop-fallback` (desktop without WebGPU → WASM/CPU) | Never — even chunked, CPU inference can take longer than the meeting |
| `< 8 GB` reported memory (Chromium `deviceMemory`) | Up to **~1 h** of audio (60 MB bytes-proxy; decode memory is the binding constraint) |
| 8 GB-class or unknown (Safari/Firefox WebGPU) | Up to **~3 h** (180 MB bytes-proxy) |

The caps protect the **decode**, not the compute: chunking bounded the per-window compute,
but `decodeAudioData` still materializes the whole recording's PCM at once. If we ever need
longer local sessions, the next step is chunked *decode* (WebCodecs), not bigger caps.

## Storage model

MediaRecorder chunks (~30 s, WebM/Opus or MP4/AAC on Safari) land in IndexedDB as they are
produced (`localAudioStore`). These are **continuation chunks** — only chunk 0 has container
headers — so per-chunk decode is not possible; that is why Stage 2 assembles the blob and
chunks at the PCM level instead. Privacy-mode rows are never auto-wiped and never show in the
orphan-resume banner; the user manages them from the Local Sessions surface.

## Related

- `docs/compute-economics.md` — why browser-first is the moat.
- `docs/always-on-recording-design.md` — the live pipeline the stop flow builds on.
- CHANGELOG 3.57.0 — the chunked-pass release notes.
