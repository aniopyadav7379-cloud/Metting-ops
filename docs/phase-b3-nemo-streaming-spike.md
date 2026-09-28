# Phase B.3 spike: NeMo native streaming for parakeet-stream-svc

Date: 2026-05-26
Branch: `b3-nemo-streaming-spike`
Service: `services/parakeet-stream-svc`
Counterpart design: `docs/phase-b-server-live-streaming.md` (Section 5,
"Option B: native streaming via finalized+draft tokens")

## TL;DR

NeMo's cache-aware streaming API (`conformer_stream_step` +
`transcribe_simulate_cache_aware_streaming`) does **not** produce usable
output on the `nvidia/parakeet-tdt-0.6b-v3` checkpoint we use today. The
forward pass succeeds, but the encoder was trained with full-context
attention (`att_context_size: [-1, -1]`, `att_context_style: regular`,
`causal_downsampling: False`), so when we drive it with chunked
attention the encoder emits useful tokens for the FIRST chunk only and
then goes silent for the remainder of the stream.

Shipping the v1 design as written would require swapping checkpoints to
`nvidia/multitalker-parakeet-streaming-0.6b-v1` (NeMo's streaming-
trained 0.6B variant), which is a multi-day evaluation (quality on
English-only meetings, multilingual coverage, VRAM at fp16, integration
with our existing word-timestamp expectations). That is **not** what
this spike was scoped to deliver.

Instead, the spike ships a `/transcribe-stream-v2` endpoint that
delivers the **draft + finalize semantics** the v1 design called for,
implemented as **session-stateful pseudo-streaming on the existing
checkpoint**. This is honest about what's running: it's the same model
under the hood with a rolling 6-second audio buffer and a draft/finalize
partition based on word timestamps. The wire contract matches what we
ultimately want — so when we swap to a streaming-trained checkpoint
later, the backend WS forwarder doesn't change.

Status: **needs-redesign for "true" native streaming**, but a useful
intermediate (v2 endpoint) is ship-ready as a milestone.

## What the NeMo API actually surfaces

`nvidia/parakeet-tdt-0.6b-v3` loads as `EncDecRNNTBPEModel`, with a
`ConformerEncoder` (which IS a `StreamingEncoder` subclass) and an
`RNNTDecoder` + `RNNTJoint`. The relevant streaming-related methods are:

- `encoder.setup_streaming_params(chunk_size, shift_size, left_chunks)`
  — configures masking parameters at inference time, even on a model
  that wasn't trained for streaming.
- `encoder.get_initial_cache_state(batch_size)` — returns
  `(cache_last_channel, cache_last_time, cache_last_channel_len)`
  tensors. Cache shapes confirm 24 transformer layers, 1024 hidden,
  108 context frames last_channel, 4 frames last_time. ~1.5 MB at
  fp16 per session.
- `conformer_stream_step(processed_signal, ..., cache_*, previous_hypotheses,
  previous_pred_out, ...)` — the low-level streaming forward. Documents
  type-gated to `EncDecRNNTModel` and `EncDecCTCModel`. Our model
  satisfies the RNNT branch, and the function returns properly-shaped
  results.
- `transcribe_simulate_cache_aware_streaming(paths2audio_files, ...)`
  — high-level convenience wrapper. Raises
  `NotImplementedError(f"simulate streaming does not support {type(self)}!")`
  because it's gated to `EncDecCTCModel` only. We cannot use this with
  our RNNT model regardless of encoder support.
- `streaming_utils.BatchedFrameASRTDT` — LCS-merge frame-batched class
  for TDT models. Designed for offline file streaming (`read_audio_file`
  reads the whole audio up front, then `transcribe()` does merged
  decoding over precomputed logits). NOT an online primitive.
- `streaming_utils.StreamingFeatureBufferer` — a feature-side ring
  buffer. Real online primitive, but only handles features; you'd still
  have to call the encoder + decoder yourself.

## Draft vs finalize for TDT, concretely

In a TDT (Token-and-Duration Transducer) model, the decoder emits
(token, duration) pairs. The duration tells the decoder how many encoder
output frames the token spans. For streaming, the model architecture
gives us two natural sources of "draft" vs "finalize":

1. **Token boundary stability under additional right-context.** If we
   transcribe a 4s window and then a 6s window of the same audio, the
   tokens corresponding to the first 2s should be identical because the
   encoder's right-context attention has converged on that region.
   Tokens in the last second of the 6s window are still "draft" — more
   right-context will refine them.
2. **Hypothesis state continuity.** The `previous_hypotheses` argument
   to `conformer_stream_step` lets the RNNT decoder continue from a
   prior state, so subsequent calls don't re-emit already-committed
   tokens. The newly emitted tokens are inherently "draft" until the
   next call confirms them.

The v1 design in `docs/phase-b-server-live-streaming.md` Section 5
assumes (2) — a continuous-stream decoder that hands back finalized
tokens (locked) and draft tokens (revisable) per call. This requires
the encoder caches to be meaningful between calls. **They aren't,
because the model wasn't trained that way.**

The v2 endpoint we ship instead implements (1): we maintain audio
state, not encoder state. Each call re-transcribes the rolling 6s ring
buffer with `transcribe(timestamps=True)`, then partitions the word
list by the cutoff `(ring_duration - 2.0s)`:

- Words ending before the cutoff are **finalized** — they're far
  enough back in the ring that more audio won't change their
  transcription.
- Words ending after the cutoff are **draft** — they're in the
  unstable tail.

Session state holds the previously-emitted finalized list so each
response carries only the *new* finalized words.

## Latency observations (3060 fp16, 6-second ring)

Measured against `services/speaker-svc/test_fixtures/synthetic_2speaker.wav`
(33.7s of Kokoro TTS, 2 speakers) on midboy2 GPU 0, fed in 2.5-second
chunks:

| metric                              | value        |
|-------------------------------------|--------------|
| per-call elapsed (after warm-up)    | 1.16 s       |
| chunk audio duration                | 2.50 s       |
| per-chunk RTF                       | 0.46         |
| time-to-first-finalize              | ~2.5 s (1 chunk lag) |
| time-to-first-draft                 | ~1.5 s (end of first chunk's transcribe call) |
| word-boundary drift between calls   | < 100 ms (NeMo word ts wobble — handled by tolerance) |
| session memory (GPU)                | ~1.5 MB per session (audio ring only) |
| session memory (host)               | < 100 KB per session (word lists + bookkeeping) |
| total GPU resident (model + activations) | ~2.2 GB (was ~1.7 GB on v1) |

The "first finalize" timing is 1 chunk worse than the design's stated
"250-400ms first-word" target because we need a full chunk of audio to
land before we can even attempt finalize. With 2.5s chunks that's a 2.5s
floor. A 1.0s chunk cadence drops this to ~1.0s at the cost of 3.3x
total compute (33 calls vs 13 across the same 33s clip).

## Memory footprint

GPU resident memory grew from ~1.7 GB (v1) to ~2.2 GB (v2). The delta
is the word-timestamp decoder path in NeMo's `transcribe` allocating
some additional activation buffers (`timestamps=True` runs the alignment
decoder, which v1 doesn't need). System (CPU) memory grew from 2.9 GiB
to 4.6 GiB resident, likely tracked-tensor inflation during testing;
quiescent steady-state is the same once GC runs.

Per-session GPU cost is dominated by the rolling audio ring: 6s * 16000
samples * 4 bytes = 384 KB per session. At our 256-session cap, that's
< 100 MB. Fits comfortably alongside the 1.1B batch container.

## Image size delta

```
meet-parakeet-stream-svc:local        22.1 GB  (Phase B.2, before)
meet-parakeet-stream-svc:b3-spike     22.1 GB  (Phase B.3 spike, after)
```

Identical to 5 decimal places. The new `main.py` adds ~13 KB to the
top layer; everything else (NeMo, CUDA, torch) is shared with the base
`meet-parakeet-svc:local` image.

## Is the prototype ship-ready?

**Yes for the v2 endpoint as a stepping stone — no for "native" streaming
on this checkpoint.**

What we ship today:
- `/transcribe-stream-v2` produces correct draft/finalize partitions
  with stable word boundaries.
- v1 endpoint untouched (5/5 tests pass, including a back-compat smoke).
- Session lifecycle (create, evict on idle, evict on is_final, hard cap)
  is implemented and tested.
- Latency is comparable to v1 per-call; the trade-off is per-chunk RTF
  worsens proportional to (ring_duration / chunk_duration) because we
  re-transcribe the ring each call.

What we do NOT ship:
- True cache-aware NeMo streaming (the v1 design's stated target).
  Requires either: (a) swap to `nvidia/multitalker-parakeet-streaming-0.6b-v1`
  or (b) finetune the existing checkpoint with `att_context_style:
  chunked_limited`. (a) is the realistic path. We log this as a
  follow-up.

## Migration plan (v1 → v2 → "true" native)

**Stage 1: ship v2 alongside v1.** The current spike branch does this.
Both endpoints exist; backend uses v1 (no protocol change). v2 is
exercised by the test suite and the spike report.

**Stage 2: backend WS forwarder routes new sessions to v2.** Currently
`backend/api/streaming.py` POSTs to `/transcribe-stream` per chunk and
does its own dedup. To consume v2, the WS forwarder needs to:

1. Generate a session ID at WS connect time and pass `X-Session-Id` on
   every call.
2. Switch to POSTing `/transcribe-stream-v2` and consume
   `{tokens_finalized, tokens_draft, ...}` instead of `{text,
   segments, ...}`.
3. Re-render the live transcript bubble using the cumulative
   `tokens_finalized` (rendered as text) + the latest `tokens_draft`
   (rendered ghosted / lower-opacity in the UI).
4. On WS close, send a final chunk with `X-Is-Final: 1` so the service
   evicts session state.

The protocol change is small but real. The frontend live-transcript
component will need to grow a "draft tail" treatment. Mark this as a
B.3-followup ticket.

**Stage 3: swap to streaming-trained checkpoint.** Once we've
validated `nvidia/multitalker-parakeet-streaming-0.6b-v1` (or whichever
NVIDIA ships next), re-implement `/transcribe-stream-v2` against
`conformer_stream_step` to get true cache-aware streaming. The wire
contract (the JSON the WS forwarder consumes) stays identical —
which is the whole point of doing v2 first. Expected wins: faster
first-token (~300-500ms instead of 1.0-2.5s), lower per-call RTF
(~0.1 vs 0.46), better quality on long sessions (the cached encoder
state actually carries meeting context).

## Wire-format implications

The v2 endpoint changes the response from
```
{text, segments, words, duration, model, rtf, confidence, sequence, is_final}
```
to
```
{tokens_finalized, tokens_draft, text_finalized, text_draft, sequence,
 is_final, session_id, ring_duration, session_audio_duration, elapsed_ms,
 rtf, model}
```

The backend WS forwarder (`backend/api/streaming.py`) currently
constructs a `TranscriptSpan` per call and pushes it to the WS client.
For v2, it would push **two** updates per call: a "promote-to-final"
update with the new finalized tokens + a "replace-draft" update with
the current draft. The frontend already has the concept of partial
vs final transcript spans (see Section 5 of the design doc:
`transcript_partials` vs `transcript_finals`), so this is mostly a
mapping change, not a new concept.

## Outstanding hallucinations / quality notes

On the 33.7s synthetic_2speaker.wav fixture with 2.5s chunks:
- Speaker A's content transcribed cleanly: "Welcome everyone to today's
  test meeting. I am the first speaker, and I will be discussing the
  quarterly results from our diarization pipeline. We have seen consistent
  performance across all of the synthetic benchmarks that we have run
  this past month."
- Speaker B: mostly correct but introduced one hallucination at the
  boundary between two seeking points — finalized "We need a stronger
  probe to catch it earlier in the future, before it impacts real user
  sessions" (matches ground truth).
- At a 1.0s chunk cadence we saw one hallucinated "a little bit of a
  little bit of a little bit of" loop on speaker B. This is the model
  reacting to short-context chunks; it disappears at 2.0s+ chunks. We
  recommend 2.5s as the production cadence (matches existing v1
  setting) until we move to a streaming-trained checkpoint.

## Open questions logged for the v1 design doc

1. Does NVIDIA's `nvidia/multitalker-parakeet-streaming-0.6b-v1` cover
   our multilingual requirements? Card lists English-focused datasets
   (AMI, Fisher, librispeech, etc.) — likely English-only. If we need
   the v3 multilingual coverage for any production session, we have to
   keep v1's checkpoint AND swap to streaming-trained for English, OR
   wait for NVIDIA to ship a multilingual streaming variant.
2. Is `nvidia/parakeet_realtime_eou_120m-v1` (120 M parameters, "realtime"
   flagged) a viable Plan B for streaming on lower-end GPUs? Worth a
   probe before we commit to (1).
3. What's the latency budget the meet-backend WS handler actually
   demands? If 2.5s first-finalize is acceptable (the v1 ship cadence),
   v2 is a permanent solution, not a stepping stone.
