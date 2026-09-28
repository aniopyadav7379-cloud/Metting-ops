# Throughput benchmark — 2026-06-08

Measured single-stream throughput for the server completion pass (STT, diarization)
plus a separate LLM tok/s measurement. All numbers below are **measured**, not modeled.

## Context: browser-first by design

Live transcription and live summarization run **in the user's browser** (in-browser
Parakeet for the live transcript + a small on-device summary LLM). The server runs
only the **per-meeting completion pass**: the canonical Parakeet 1.1B transcription,
pyannote 3.1 diarization, a Qwen 3.6 35B-A3B-Vision summary/title, and the cross-meeting
search index. So these throughput numbers describe a once-per-finished-meeting batch job,
not a continuous per-user-hour cost.

## Test clip

- Warm services, single run, **clean single-speaker** audio clip, **~263 s** long.
- "Warm" = models already loaded; this is the steady-state path, not cold-start.

## STT — Parakeet 1.1B (midboy2, RTX 3060)

| Metric | Value |
|---|---|
| Audio duration | 263 s |
| Wall-clock | 4.40 s |
| RTF | ≈ 0.017 (~60× realtime) |
| Projected 30-min meeting | ~30 s |
| Single-stream throughput | ~120 meetings/hour |

## Diarization — pyannote 3.1 (bigboy, RTX 3090)

| Metric | Value |
|---|---|
| Audio duration | 263 s |
| Wall-clock | 4.17 s |
| RTF | ≈ 0.016 |
| Projected 30-min meeting | ~30 s |
| Single-stream throughput | ~124 meetings/hour |

GPU power during the run: the RTX 3090 briefly hit **239 W** during the ~4 s job and sat
at **~29 W** idle otherwise — i.e. the diarization job is a short, bursty load on the card.

## LLM — Qwen 3.6 35B-A3B (Q4_K_M, measured separately)

Generation throughput (tokens/sec), measured separately from the clip above:

| GPU | Single stream | 8 concurrent |
|---|---|---|
| RTX 3090 | 148 tok/s | 327 tok/s |
| RTX 6000 | 102 tok/s | 195 tok/s |
| Tesla P40 | ~80 tok/s | — |

## Caveat

**Clean single-speaker audio is a best-case floor.** Real multi-speaker meetings make
pyannote work harder (more speech-segment boundaries, more clustering, more embedding
work), so the ~30 s-per-30-min-meeting diarization figure should be treated as a **floor**,
not a typical or worst case. STT is far less sensitive to speaker count.

## Conclusion

Both STT (~30 s) and diarization (~30 s) clear a 30-minute meeting in about **30 seconds**
single-stream, so the server completion pass has **large headroom** — a single stream
alone handles ~120 meetings/hour of recorded duration, and the LLM summary step is a short
generation on top of that. The practical concurrency limiter is **not** raw model speed but
the speaker-svc's **single uvicorn worker** (one in-flight diarization at a time), tracked
separately. Scaling concurrency means scaling that worker, not the GPU compute budget.
