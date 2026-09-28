/**
 * Tests for opusEncoder.
 *
 * Vitest runs under jsdom, which provides neither `WebCodecs.AudioEncoder`
 * nor a real `MediaRecorder` that can encode Opus. We test:
 *
 *   1. The surface contract — exported types, factory signature, error type
 *   2. Internal helpers — int16ToFloat32, resampleFloat32 (pure functions)
 *   3. Detection logic — feature detection returns false in jsdom
 *   4. Error type — OpusUnsupportedError is throwable / instanceof checks
 *   5. Mocked path — install fake AudioEncoder + AudioData on globalThis
 *      and verify the factory wires up correctly.
 *
 * We deliberately do NOT roundtrip through a real Opus encoder here;
 * that's covered by the Python decoder tests and will be covered
 * end-to-end at integration time.
 */
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type MockInstance,
} from "vitest";

import {
  createOpusEncoder,
  OpusUnsupportedError,
  __internal,
} from "../opusEncoder";

// -----------------------------------------------------------------------------
// Surface + error type
// -----------------------------------------------------------------------------

describe("opusEncoder surface", () => {
  it("OpusUnsupportedError extends Error with the right name", () => {
    const e = new OpusUnsupportedError("nope");
    expect(e).toBeInstanceOf(Error);
    expect(e).toBeInstanceOf(OpusUnsupportedError);
    expect(e.name).toBe("OpusUnsupportedError");
    expect(e.message).toBe("nope");
  });

  it("rejects bad sampleRate at the factory boundary", async () => {
    await expect(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      createOpusEncoder({ sampleRate: 22050 as any }),
    ).rejects.toThrow(/sampleRate must be/);
  });
});

// -----------------------------------------------------------------------------
// Internal pure helpers
// -----------------------------------------------------------------------------

describe("int16ToFloat32", () => {
  it("converts max-magnitude samples to ~±1", () => {
    const samples = new Int16Array([32767, -32768, 0, 16384, -16384]);
    const out = __internal.int16ToFloat32(samples);
    expect(out.length).toBe(5);
    expect(out[0]).toBeCloseTo(0.9999, 3);
    expect(out[1]).toBeCloseTo(-1.0, 3);
    expect(out[2]).toBe(0);
    expect(out[3]).toBeCloseTo(0.5, 3);
    expect(out[4]).toBeCloseTo(-0.5, 3);
  });

  it("returns an empty array on empty input", () => {
    expect(__internal.int16ToFloat32(new Int16Array(0)).length).toBe(0);
  });
});

describe("resampleFloat32", () => {
  it("returns the input untouched at matching rates", () => {
    const f = new Float32Array([0.1, -0.2, 0.3]);
    const out = __internal.resampleFloat32(f, 48000, 48000);
    expect(out).toBe(f);
  });

  it("3x upsample produces 3x the length", () => {
    const f = new Float32Array(160); // 10ms @ 16k
    for (let i = 0; i < f.length; i++) f[i] = Math.sin(i / 10);
    const out = __internal.resampleFloat32(f, 16000, 48000);
    // 160 input * (48000/16000) = 480 — give or take floor rounding
    expect(out.length).toBe(480);
  });

  it("3x downsample produces 1/3 the length", () => {
    const f = new Float32Array(480);
    for (let i = 0; i < f.length; i++) f[i] = i / 480;
    const out = __internal.resampleFloat32(f, 48000, 16000);
    expect(out.length).toBe(160);
  });

  it("preserves first sample", () => {
    const f = new Float32Array([0.5, 0.6, 0.7, 0.8]);
    const out = __internal.resampleFloat32(f, 16000, 48000);
    expect(out[0]).toBeCloseTo(0.5, 3);
  });
});

// -----------------------------------------------------------------------------
// Feature detection in jsdom
// -----------------------------------------------------------------------------

describe("feature detection in jsdom", () => {
  it("isWebCodecsOpusSupported returns false (no AudioEncoder in jsdom)", () => {
    expect(__internal.isWebCodecsOpusSupported()).toBe(false);
  });

  it("isMediaRecorderOpusSupported returns false (jsdom MediaRecorder is incomplete)", () => {
    // jsdom may or may not stub MediaRecorder; in either case the
    // helper should return false when isTypeSupported isn't a
    // function or returns false for opus.
    const supported = __internal.isMediaRecorderOpusSupported();
    expect(typeof supported).toBe("boolean");
    expect(supported).toBe(false);
  });
});

// -----------------------------------------------------------------------------
// Factory falls through to OpusUnsupportedError in jsdom
// -----------------------------------------------------------------------------

describe("createOpusEncoder fallback behavior", () => {
  it("throws OpusUnsupportedError when nothing is available", async () => {
    await expect(
      createOpusEncoder({ sampleRate: 16000 }),
    ).rejects.toBeInstanceOf(OpusUnsupportedError);
  });

  it("preferImplementation=webcodecs throws OpusUnsupportedError in jsdom", async () => {
    await expect(
      createOpusEncoder({
        sampleRate: 16000,
        preferImplementation: "webcodecs",
      }),
    ).rejects.toBeInstanceOf(OpusUnsupportedError);
  });

  it("preferImplementation=mediarecorder throws OpusUnsupportedError in jsdom", async () => {
    await expect(
      createOpusEncoder({
        sampleRate: 16000,
        preferImplementation: "mediarecorder",
      }),
    ).rejects.toBeInstanceOf(OpusUnsupportedError);
  });
});

// -----------------------------------------------------------------------------
// Mocked WebCodecs path
// -----------------------------------------------------------------------------

describe("createOpusEncoder with mocked WebCodecs", () => {
  // We mock AudioEncoder + AudioData on globalThis to simulate a
  // WebCodecs-capable browser. The mock captures calls and synthesizes
  // an Opus packet (8 random bytes) on each encode.

  type MockEncoderInit = {
    output: (chunk: { byteLength: number; copyTo: (buf: Uint8Array) => void }) => void;
    error: (e: Error) => void;
  };

  let installedKeys: string[] = [];
  let encodeSpy: MockInstance;
  let configureSpy: MockInstance;

  beforeEach(() => {
    installedKeys = [];
    encodeSpy = vi.fn();
    configureSpy = vi.fn();

    const isConfigSupported = vi.fn().mockResolvedValue({ supported: true });

    class MockAudioEncoder {
      static isConfigSupported = isConfigSupported;
      private outputCb: MockEncoderInit["output"];
      constructor(init: MockEncoderInit) {
        this.outputCb = init.output;
      }
      configure(cfg: unknown) {
        configureSpy(cfg);
      }
      encode(data: unknown) {
        encodeSpy(data);
        // Synthesize an Opus packet via the output callback.
        const fakePacket = new Uint8Array([0xfc, 0x01, 0x02, 0x03]);
        this.outputCb({
          byteLength: fakePacket.length,
          copyTo: (buf: Uint8Array) => {
            buf.set(fakePacket);
          },
        });
      }
      async flush() {
        // no-op
      }
      close() {
        // no-op
      }
    }

    class MockAudioData {
      sampleRate: number;
      numberOfFrames: number;
      numberOfChannels: number;
      timestamp: number;
      format: string;
      constructor(init: {
        format: string;
        sampleRate: number;
        numberOfFrames: number;
        numberOfChannels: number;
        timestamp: number;
        data: Float32Array;
      }) {
        this.format = init.format;
        this.sampleRate = init.sampleRate;
        this.numberOfFrames = init.numberOfFrames;
        this.numberOfChannels = init.numberOfChannels;
        this.timestamp = init.timestamp;
      }
      close() {
        // no-op
      }
    }

    const g = globalThis as unknown as Record<string, unknown>;
    if (!("AudioEncoder" in g)) {
      g["AudioEncoder"] = MockAudioEncoder;
      installedKeys.push("AudioEncoder");
    }
    if (!("AudioData" in g)) {
      g["AudioData"] = MockAudioData;
      installedKeys.push("AudioData");
    }
  });

  afterEach(() => {
    const g = globalThis as unknown as Record<string, unknown>;
    for (const k of installedKeys) {
      delete g[k];
    }
    installedKeys = [];
    vi.restoreAllMocks();
  });

  it("creates a WebCodecs-backed encoder when AudioEncoder is present", async () => {
    const enc = await createOpusEncoder({ sampleRate: 16000 });
    expect(enc.implementation).toBe("webcodecs");
    expect(enc.inputSampleRate).toBe(16000);
    expect(enc.bitrate).toBe(24000);
    await enc.close();
  });

  it("respects a custom bitrate", async () => {
    const enc = await createOpusEncoder({ sampleRate: 16000, bitrate: 32000 });
    expect(enc.bitrate).toBe(32000);
    await enc.close();
  });

  it("encode() returns a Uint8Array packet", async () => {
    const enc = await createOpusEncoder({ sampleRate: 16000 });
    // 20ms @ 16kHz = 320 samples
    const pcm = new Int16Array(320);
    for (let i = 0; i < pcm.length; i++) {
      pcm[i] = Math.floor(Math.sin(i / 5) * 16384);
    }
    const packet = await enc.encode(pcm);
    expect(packet).toBeInstanceOf(Uint8Array);
    expect(packet!.length).toBeGreaterThan(0);
    await enc.close();
  });

  it("encode() returns null on empty input", async () => {
    const enc = await createOpusEncoder({ sampleRate: 16000 });
    const packet = await enc.encode(new Int16Array(0));
    expect(packet).toBeNull();
    await enc.close();
  });

  it("upsamples 16k -> 48k before handing to the encoder", async () => {
    const enc = await createOpusEncoder({ sampleRate: 16000 });
    // 10ms @ 16kHz = 160 input samples; should be encoded as 480 @ 48kHz
    await enc.encode(new Int16Array(160));
    expect(encodeSpy).toHaveBeenCalledTimes(1);
    const audioData = encodeSpy.mock.calls[0]![0] as {
      numberOfFrames: number;
      sampleRate: number;
    };
    expect(audioData.sampleRate).toBe(48000);
    expect(audioData.numberOfFrames).toBe(480);
    await enc.close();
  });

  it("passes through 48k input without resampling", async () => {
    const enc = await createOpusEncoder({ sampleRate: 48000 });
    // 10ms @ 48kHz = 480 input samples; should stay at 480
    await enc.encode(new Int16Array(480));
    expect(encodeSpy).toHaveBeenCalledTimes(1);
    const audioData = encodeSpy.mock.calls[0]![0] as {
      numberOfFrames: number;
      sampleRate: number;
    };
    expect(audioData.sampleRate).toBe(48000);
    expect(audioData.numberOfFrames).toBe(480);
    await enc.close();
  });

  it("close() makes subsequent encode() throw", async () => {
    const enc = await createOpusEncoder({ sampleRate: 16000 });
    await enc.close();
    await expect(enc.encode(new Int16Array(160))).rejects.toThrow(/closed/);
  });

  it("calls configure() with codec=opus and the right rate", async () => {
    await createOpusEncoder({ sampleRate: 16000, bitrate: 24000 });
    expect(configureSpy).toHaveBeenCalledTimes(1);
    const cfg = configureSpy.mock.calls[0]![0] as {
      codec: string;
      sampleRate: number;
      numberOfChannels: number;
      bitrate: number;
    };
    expect(cfg.codec).toBe("opus");
    expect(cfg.sampleRate).toBe(48000);
    expect(cfg.numberOfChannels).toBe(1);
    expect(cfg.bitrate).toBe(24000);
  });
});
