/**
 * Opus encoder utility — Phase B.3 chunk B.
 *
 * The streaming WS uplink currently sends raw PCM16 at 16 kHz mono
 * (32 KB/s). Switching to Opus at 24 kbps shrinks the wire payload by
 * ~8-16x — meaningful for mobile uplink, weak networks, and battery.
 *
 * Two implementation paths
 * ------------------------
 *   WebCodecs.AudioEncoder (preferred)
 *     - Chrome 94+, Edge, Opera (Chromium-based browsers).
 *     - Produces raw Opus packets — no container — which is exactly
 *       what our WS protocol's "OPUS" payload slot expects (per
 *       docs/phase-b-server-live-streaming.md section 4 line 289).
 *     - Lowest CPU, best compression knob granularity.
 *
 *   MediaRecorder (fallback)
 *     - Firefox 47+, Safari 14.1+, Chrome (also supported but we
 *       prefer WebCodecs there).
 *     - Produces audio/webm; codecs=opus — a WebM container with Opus
 *       inside, NOT bare Opus packets. The server has to either
 *       demux WebM or accept the container as the wire format.
 *       See README in opus_decoder.py for the matching server side.
 *     - This path is here so non-Chromium browsers can still benefit;
 *       the integration step (not this PR) decides whether to enable
 *       it by default or always fall back to PCM16 on browsers that
 *       can't do WebCodecs.
 *
 *   Final fallback: throws OpusUnsupportedError so the caller can fall
 *   back to PCM16 over the existing WS path.
 *
 * Surface contract
 * ----------------
 *   const enc = await createOpusEncoder({ sampleRate: 16000 });
 *   const pkt = await enc.encode(int16Pcm);  // one packet or null
 *   const tail = await enc.flush();          // drain
 *   await enc.close();
 *
 *   enc.implementation === 'webcodecs' | 'mediarecorder'
 *
 * The encoder accepts PCM16 Int16Array (matches what an AudioWorklet
 * emits per Phase B.3 chunk A) at 16 kHz or 48 kHz. Internally we
 * upsample to 48 kHz before handing it to Opus (Opus' native rate).
 */

// -----------------------------------------------------------------------------
// Public types
// -----------------------------------------------------------------------------

export class OpusUnsupportedError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OpusUnsupportedError";
  }
}

export interface OpusEncoder {
  /**
   * Encode a chunk of PCM16 samples. Returns one Opus packet when the
   * encoder has buffered enough input for a frame; otherwise `null`
   * (caller should keep feeding).
   *
   * WebCodecs path emits packets per-frame (20 ms default); the
   * MediaRecorder path emits chunks at the timeslice the recorder was
   * started with (we use 20 ms but the browser may aggregate).
   */
  encode(pcm16: Int16Array): Promise<Uint8Array | null>;

  /**
   * Drain any buffered packets and return them. After flush(), the
   * encoder is still usable. Returns [] if nothing was buffered.
   */
  flush(): Promise<Uint8Array[]>;

  /**
   * Release codec resources. After close(), encode/flush will reject.
   */
  close(): Promise<void>;

  /** Which path is active. */
  readonly implementation: "webcodecs" | "mediarecorder";

  /** Source sample rate the encoder accepts (16000 or 48000). */
  readonly inputSampleRate: 16000 | 48000;

  /** Configured target bitrate (bps). */
  readonly bitrate: number;
}

export interface CreateOpusEncoderOptions {
  /**
   * Sample rate of the PCM16 the caller is feeding in. Must be 16 kHz
   * (Parakeet's wire rate) or 48 kHz (passthrough).
   */
  sampleRate: 16000 | 48000;

  /** Target bitrate in bits per second. Default 24000 (speech VoIP). */
  bitrate?: number;

  /**
   * Force a specific implementation. Useful for testing or to skip
   * WebCodecs if the caller has a reason to prefer MediaRecorder.
   */
  preferImplementation?: "webcodecs" | "mediarecorder" | "auto";
}

// -----------------------------------------------------------------------------
// Public factory
// -----------------------------------------------------------------------------

/**
 * Build an Opus encoder using whichever browser API is available.
 *
 * Detection order
 *   1. WebCodecs.AudioEncoder with `codec: 'opus'` — preferred.
 *   2. MediaRecorder with `audio/webm; codecs=opus` — fallback.
 *   3. Throw OpusUnsupportedError.
 *
 * Caller is expected to catch OpusUnsupportedError and fall back to
 * raw PCM16 over the existing WS path. We do NOT silently fall back
 * to PCM16 here — the caller controls the wire format and needs to
 * know which "format" slot in the WS header to write.
 */
export async function createOpusEncoder(
  opts: CreateOpusEncoderOptions,
): Promise<OpusEncoder> {
  const { sampleRate } = opts;
  if (sampleRate !== 16000 && sampleRate !== 48000) {
    throw new Error(`sampleRate must be 16000 or 48000, got ${sampleRate}`);
  }
  const bitrate = opts.bitrate ?? 24000;
  const prefer = opts.preferImplementation ?? "auto";

  // Path 1: WebCodecs
  if (prefer === "webcodecs" || prefer === "auto") {
    if (isWebCodecsOpusSupported()) {
      try {
        return await createWebCodecsEncoder({ sampleRate, bitrate });
      } catch (e) {
        if (prefer === "webcodecs") throw e;
        // else fall through to MediaRecorder
      }
    } else if (prefer === "webcodecs") {
      throw new OpusUnsupportedError(
        "WebCodecs AudioEncoder is not available in this environment",
      );
    }
  }

  // Path 2: MediaRecorder
  if (prefer === "mediarecorder" || prefer === "auto") {
    if (isMediaRecorderOpusSupported()) {
      return await createMediaRecorderEncoder({ sampleRate, bitrate });
    } else if (prefer === "mediarecorder") {
      throw new OpusUnsupportedError(
        "MediaRecorder with audio/webm;codecs=opus is not available",
      );
    }
  }

  // Path 3: nothing works
  throw new OpusUnsupportedError(
    "Neither WebCodecs nor MediaRecorder support Opus in this environment; " +
      "caller should fall back to PCM16",
  );
}

// -----------------------------------------------------------------------------
// Feature detection
// -----------------------------------------------------------------------------

function isWebCodecsOpusSupported(): boolean {
  if (typeof globalThis === "undefined") return false;
  const g = globalThis as unknown as { AudioEncoder?: unknown };
  if (typeof g.AudioEncoder !== "function") return false;
  // The constructor exists; we'd ideally call AudioEncoder.isConfigSupported
  // here but it's async and feature-detection should be cheap. Defer the
  // actual config check to createWebCodecsEncoder, which will throw
  // and let us fall through to MediaRecorder.
  return true;
}

function isMediaRecorderOpusSupported(): boolean {
  if (typeof MediaRecorder === "undefined") return false;
  if (typeof MediaRecorder.isTypeSupported !== "function") return false;
  return (
    MediaRecorder.isTypeSupported("audio/webm; codecs=opus") ||
    MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
  );
}

// -----------------------------------------------------------------------------
// Shared helpers
// -----------------------------------------------------------------------------

const OPUS_INTERNAL_RATE = 48000;

/**
 * Convert Int16 PCM to Float32 in [-1, 1].
 */
function int16ToFloat32(samples: Int16Array): Float32Array {
  const out = new Float32Array(samples.length);
  for (let i = 0; i < samples.length; i++) {
    const s = samples[i] ?? 0;
    // Divide by 32768 (max negative magnitude) to map to [-1, 1).
    out[i] = s / 32768;
  }
  return out;
}

/**
 * Linear-interpolation upsample from sourceRate -> targetRate.
 *
 * For 16000 -> 48000 this is an exact 3x integer upsample. We still
 * use a linear formulation so the function works for any pair the
 * caller might pass. Opus' own resampler will smooth out the
 * artifacts internally; for speech-band audio this is fine. If we
 * needed bit-perfect SNR we'd use a polyphase FIR — overkill here.
 */
function resampleFloat32(
  input: Float32Array,
  sourceRate: number,
  targetRate: number,
): Float32Array {
  if (sourceRate === targetRate) return input;
  const ratio = sourceRate / targetRate;
  const outLength = Math.floor(input.length / ratio);
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const srcIdx = i * ratio;
    const lo = Math.floor(srcIdx);
    const hi = Math.min(lo + 1, input.length - 1);
    const t = srcIdx - lo;
    const a = input[lo] ?? 0;
    const b = input[hi] ?? 0;
    out[i] = a * (1 - t) + b * t;
  }
  return out;
}

// -----------------------------------------------------------------------------
// WebCodecs implementation
// -----------------------------------------------------------------------------

/**
 * Sample-accurate timestamp accumulator. The WebCodecs AudioEncoder
 * needs `timestamp` (microseconds since stream start) on every input;
 * if we get it wrong the encoder will silently drop frames.
 */
class TimestampClock {
  private samplesEmitted = 0;
  constructor(private readonly sampleRate: number) {}
  next(samples: number): number {
    const ts = Math.floor((this.samplesEmitted * 1e6) / this.sampleRate);
    this.samplesEmitted += samples;
    return ts;
  }
}

async function createWebCodecsEncoder(opts: {
  sampleRate: 16000 | 48000;
  bitrate: number;
}): Promise<OpusEncoder> {
  // We always run the codec at 48 kHz (Opus' native rate). If the
  // caller is feeding 16 kHz, we upsample.
  const codecRate = OPUS_INTERNAL_RATE;
  const config: AudioEncoderConfig = {
    codec: "opus",
    sampleRate: codecRate,
    numberOfChannels: 1,
    bitrate: opts.bitrate,
  };

  // Verify the config is actually supported before allocating the
  // encoder. AudioEncoder.isConfigSupported is the proper way to
  // detect; without it we'd get a confusing async error later.
  const support = await (
    globalThis as unknown as {
      AudioEncoder: {
        isConfigSupported: (c: AudioEncoderConfig) => Promise<{
          supported?: boolean;
        }>;
      };
    }
  ).AudioEncoder.isConfigSupported(config);
  if (!support.supported) {
    throw new OpusUnsupportedError(
      "WebCodecs AudioEncoder.isConfigSupported returned false for opus@48k",
    );
  }

  const pending: Uint8Array[] = [];
  let lastError: Error | null = null;

  // AudioEncoder is a constructor on globalThis in browsers that
  // support it. We dispatch via globalThis so the file still type-
  // checks in node where lib.dom.d.ts may or may not be loaded.
  const AudioEncoderCtor = (
    globalThis as unknown as { AudioEncoder: new (init: AudioEncoderInit) => AudioEncoder }
  ).AudioEncoder;
  const AudioDataCtor = (
    globalThis as unknown as { AudioData: new (init: AudioDataInit) => AudioData }
  ).AudioData;

  const encoder = new AudioEncoderCtor({
    output: (chunk: EncodedAudioChunk) => {
      const buf = new Uint8Array(chunk.byteLength);
      chunk.copyTo(buf);
      pending.push(buf);
    },
    error: (e: DOMException | Error) => {
      lastError = e instanceof Error ? e : new Error(String(e));
    },
  });

  encoder.configure(config);

  const clock = new TimestampClock(codecRate);
  let closed = false;

  return {
    implementation: "webcodecs",
    inputSampleRate: opts.sampleRate,
    bitrate: opts.bitrate,

    async encode(pcm16: Int16Array): Promise<Uint8Array | null> {
      if (closed) throw new Error("encoder is closed");
      if (lastError) throw lastError;
      if (pcm16.length === 0) return null;

      // PCM16 -> Float32 [-1,1] -> upsample to 48 kHz if needed
      const f32 = int16ToFloat32(pcm16);
      const upsampled =
        opts.sampleRate === codecRate
          ? f32
          : resampleFloat32(f32, opts.sampleRate, codecRate);

      const ts = clock.next(upsampled.length);
      const data = new AudioDataCtor({
        format: "f32",
        sampleRate: codecRate,
        numberOfFrames: upsampled.length,
        numberOfChannels: 1,
        timestamp: ts,
        data: upsampled,
      });
      try {
        encoder.encode(data);
      } finally {
        data.close();
      }

      // The encoder emits packets asynchronously via the `output`
      // callback. We let any already-pending packets surface.
      if (pending.length > 0) {
        return pending.shift()!;
      }
      return null;
    },

    async flush(): Promise<Uint8Array[]> {
      if (closed) throw new Error("encoder is closed");
      if (lastError) throw lastError;
      await encoder.flush();
      const drained = pending.splice(0, pending.length);
      return drained;
    },

    async close(): Promise<void> {
      if (closed) return;
      closed = true;
      try {
        await encoder.flush();
      } catch {
        // best-effort; close should not throw
      }
      try {
        encoder.close();
      } catch {
        // close on an already-broken encoder can throw
      }
    },
  };
}

// -----------------------------------------------------------------------------
// MediaRecorder implementation
// -----------------------------------------------------------------------------

/**
 * MediaRecorder path. The wire format is `audio/webm; codecs=opus` —
 * a WebM container wrapping Opus, NOT bare Opus packets. The decoder
 * side must either demux the WebM container OR (simpler) treat the
 * entire blob as the payload and rely on the recipient using a
 * MediaSource / decodeAudioData to extract.
 *
 * Why we use it: Firefox and Safari don't have WebCodecs.AudioEncoder.
 * MediaRecorder gives us *some* Opus compression even if the framing
 * is uglier. For the v1 integration this path will probably be wired
 * as "if WebCodecs is unavailable, fall back to PCM16" rather than
 * shipping WebM-over-WS — but the path exists for completeness.
 *
 * Important caveat documented in the file header.
 */
async function createMediaRecorderEncoder(opts: {
  sampleRate: 16000 | 48000;
  bitrate: number;
}): Promise<OpusEncoder> {
  // We need a MediaStream to feed MediaRecorder. Since the caller is
  // giving us PCM16 directly (no live mic), we synthesize a stream by
  // building a Web Audio graph: ScriptProcessor / MediaStreamDestination.
  // This is a thin shim — production code can call MediaRecorder on
  // the live mic directly without going through this encoder.

  if (typeof AudioContext === "undefined") {
    throw new OpusUnsupportedError(
      "AudioContext is unavailable; MediaRecorder Opus path requires Web Audio",
    );
  }

  const ctx = new AudioContext({ sampleRate: OPUS_INTERNAL_RATE });
  const dest = ctx.createMediaStreamDestination();

  // Buffer source: we'll feed each encode() call as a transient
  // AudioBufferSourceNode connected to `dest`. Crude but works.
  const stream = dest.stream;

  const mimeType = "audio/webm; codecs=opus";
  if (!MediaRecorder.isTypeSupported(mimeType)) {
    throw new OpusUnsupportedError(`MediaRecorder rejects ${mimeType}`);
  }

  const recorder = new MediaRecorder(stream, {
    mimeType,
    audioBitsPerSecond: opts.bitrate,
  });

  const pending: Uint8Array[] = [];
  let lastError: Error | null = null;

  recorder.addEventListener("dataavailable", (e: BlobEvent) => {
    if (e.data && e.data.size > 0) {
      e.data
        .arrayBuffer()
        .then((buf) => pending.push(new Uint8Array(buf)))
        .catch((err) => {
          lastError = err instanceof Error ? err : new Error(String(err));
        });
    }
  });
  recorder.addEventListener("error", (e: Event) => {
    const ev = e as Event & { error?: Error };
    lastError = ev.error ?? new Error("MediaRecorder error");
  });

  // 20 ms timeslice — matches the WebCodecs frame budget. The browser
  // may aggregate (Firefox in particular tends to deliver larger
  // chunks than the timeslice), so callers should not rely on exact
  // 20 ms framing on the wire.
  recorder.start(20);

  let closed = false;

  return {
    implementation: "mediarecorder",
    inputSampleRate: opts.sampleRate,
    bitrate: opts.bitrate,

    async encode(pcm16: Int16Array): Promise<Uint8Array | null> {
      if (closed) throw new Error("encoder is closed");
      if (lastError) throw lastError;
      if (pcm16.length === 0) return null;

      // PCM16 -> Float32 -> AudioBuffer at codec rate
      const f32 = int16ToFloat32(pcm16);
      const upsampled =
        opts.sampleRate === OPUS_INTERNAL_RATE
          ? f32
          : resampleFloat32(f32, opts.sampleRate, OPUS_INTERNAL_RATE);

      const buf = ctx.createBuffer(1, upsampled.length, OPUS_INTERNAL_RATE);
      buf.copyToChannel(upsampled, 0);
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(dest);
      src.start();

      // Try to deliver an already-buffered chunk; the recorder fires
      // dataavailable asynchronously on its own schedule.
      if (pending.length > 0) {
        return pending.shift()!;
      }
      return null;
    },

    async flush(): Promise<Uint8Array[]> {
      if (closed) throw new Error("encoder is closed");
      if (lastError) throw lastError;
      // requestData forces a dataavailable event with whatever's
      // queued. Wait one event-loop turn so the blob lands in `pending`.
      recorder.requestData();
      await new Promise((r) => setTimeout(r, 0));
      return pending.splice(0, pending.length);
    },

    async close(): Promise<void> {
      if (closed) return;
      closed = true;
      try {
        if (recorder.state !== "inactive") recorder.stop();
      } catch {
        // ignore
      }
      try {
        await ctx.close();
      } catch {
        // ignore
      }
    },
  };
}

// -----------------------------------------------------------------------------
// Re-exports for tests + diagnostics
// -----------------------------------------------------------------------------

/**
 * Internal helpers exposed for unit tests. Not part of the stable
 * public API; do not consume from outside this module's tests.
 */
export const __internal = {
  int16ToFloat32,
  resampleFloat32,
  isWebCodecsOpusSupported,
  isMediaRecorderOpusSupported,
};
