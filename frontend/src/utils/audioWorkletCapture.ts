/**
 * Phase B.3 audio-capture helper.
 *
 * Wraps the `pcm16-encoder` AudioWorklet shipped under
 * `frontend/public/audio-worklets/pcm16-encoder.worklet.js` and presents
 * a single function-style API to the caller:
 *
 *     const cap = await startPcm16Capture(stream, onFrame, { ... });
 *     ...
 *     cap.stop();
 *
 * On modern browsers (Chrome 64+, Firefox 76+, Safari 14.1+) it uses
 * an AudioWorkletNode which runs in a dedicated audio thread, avoiding
 * the jank that ScriptProcessorNode caused on the main thread.
 *
 * On older browsers without `audioContext.audioWorklet`, it transparently
 * falls back to ScriptProcessorNode with a console warning. The fallback
 * exists so the page degrades rather than breaks on legacy Safari, but
 * we expect ~0 real-world usage by 2026.
 *
 * Output contract is identical in both paths: callbacks receive an
 * `Int16Array` of native-rate (16 kHz, mono) PCM16 samples whose length
 * equals the `frameSamples` config option (default 3200 = 200 ms at
 * 16 kHz). Bit-exact compatibility with the existing wire format is
 * preserved, so the 19-byte BE header builder in StreamingTest.tsx does
 * not need to change.
 *
 * Why caller-owned MediaStream:
 *   The caller still owns the `getUserMedia` MediaStream lifecycle and
 *   the AudioContext. We only attach the worklet (or ScriptProcessor)
 *   onto a source node we build internally. This matches the pattern
 *   that StreamingTest.tsx already had, so swapping in this helper is
 *   a straight replacement of the inner loop, not a lifecycle rewrite.
 */

export interface Pcm16CaptureOptions {
  /** Target sample rate. Defaults to 16000 Hz to match the wire format. */
  sampleRate?: number;
  /** Samples per emitted frame. Default 3200 (200 ms at 16 kHz). */
  frameSamples?: number;
  /**
   * URL of the worklet module. Defaults to the path Vite serves the
   * file from when it's placed under `public/audio-worklets/`. Tests
   * can override this to load a stub.
   */
  workletUrl?: string;
  /**
   * Override the AudioContext. The caller may want to reuse an
   * existing context (e.g., one already used by the VU meter) but in
   * StreamingTest's case we mint a fresh one per session and close it
   * on stop. Either is fine; if absent, we build our own.
   */
  audioContext?: AudioContext;
}

export interface Pcm16Capture {
  /**
   * Tear down the worklet (or ScriptProcessor), the source node, and
   * the AudioContext (if we created it). Idempotent.
   */
  stop: () => void;
  /**
   * True if we ended up on the legacy ScriptProcessorNode path. Callers
   * may want to surface this in the UI ("running on the legacy capture
   * path; please upgrade Safari").
   */
  readonly usingFallback: boolean;
}

export type Pcm16FrameHandler = (frame: Int16Array) => void;

const DEFAULT_FRAME_SAMPLES = 3200; // 200 ms at 16 kHz
const DEFAULT_SAMPLE_RATE = 16000;
const DEFAULT_WORKLET_URL = '/audio-worklets/pcm16-encoder.worklet.js';

interface AudioContextCtor {
  new (options?: AudioContextOptions): AudioContext;
}

function getAudioContextCtor(): AudioContextCtor | null {
  const w = window as unknown as {
    AudioContext?: AudioContextCtor;
    webkitAudioContext?: AudioContextCtor;
  };
  return w.AudioContext ?? w.webkitAudioContext ?? null;
}

/**
 * Start capturing PCM16 frames from a MediaStream. Resolves once the
 * worklet (or ScriptProcessor) is wired up and producing audio.
 *
 * Throws on:
 *   - No usable AudioContext constructor in the browser (very old IE
 *     etc.); not recoverable.
 *   - Worklet module failing to load (network error, MIME mismatch);
 *     we then attempt the ScriptProcessor fallback transparently.
 *   - getUserMedia stream having no audio track; caller is expected
 *     to validate before calling this helper.
 */
export async function startPcm16Capture(
  stream: MediaStream,
  onFrame: Pcm16FrameHandler,
  opts: Pcm16CaptureOptions = {}
): Promise<Pcm16Capture> {
  const sampleRate = opts.sampleRate ?? DEFAULT_SAMPLE_RATE;
  const frameSamples = opts.frameSamples ?? DEFAULT_FRAME_SAMPLES;
  const workletUrl = opts.workletUrl ?? DEFAULT_WORKLET_URL;

  const Ctor = getAudioContextCtor();
  if (!Ctor) {
    throw new Error('AudioContext is not supported by this browser');
  }

  const ownedContext = !opts.audioContext;
  const audioContext = opts.audioContext ?? new Ctor({ sampleRate });
  const source = audioContext.createMediaStreamSource(stream);

  // Some browsers ship `audioWorklet` but disable it under cross-
  // origin-isolation restrictions; check for the addModule method too.
  const hasWorklet =
    typeof audioContext.audioWorklet !== 'undefined' &&
    typeof audioContext.audioWorklet.addModule === 'function';

  if (hasWorklet) {
    try {
      return await startWorkletCapture(
        audioContext,
        source,
        workletUrl,
        frameSamples,
        onFrame,
        ownedContext
      );
    } catch (err) {
      // Worklet failed to load (404, MIME error, parse error). Drop
      // back to the deprecated path so the page still works in
      // worst-case dev/network scenarios.
      console.warn(
        '[audioWorkletCapture] AudioWorklet load failed, falling back to ' +
          'ScriptProcessorNode. This is the legacy capture path and runs ' +
          'on the main thread. Error:',
        err
      );
    }
  } else {
    console.warn(
      '[audioWorkletCapture] AudioContext.audioWorklet is undefined ' +
        '(Safari < 14.1 or an older browser). Falling back to the ' +
        'deprecated ScriptProcessorNode capture path. Audio may glitch ' +
        'under main-thread load.'
    );
  }

  return startScriptProcessorFallback(
    audioContext,
    source,
    frameSamples,
    onFrame,
    ownedContext
  );
}

/**
 * Internal: wire up the AudioWorkletNode. Caller has already verified
 * `audioContext.audioWorklet` exists. The worklet module URL is fetched
 * over the network on first call per AudioContext.
 */
async function startWorkletCapture(
  audioContext: AudioContext,
  source: MediaStreamAudioSourceNode,
  workletUrl: string,
  frameSamples: number,
  onFrame: Pcm16FrameHandler,
  ownedContext: boolean
): Promise<Pcm16Capture> {
  await audioContext.audioWorklet.addModule(workletUrl);

  const node = new AudioWorkletNode(audioContext, 'pcm16-encoder', {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
  });

  node.port.postMessage({ type: 'config', frameSamples });
  node.port.onmessage = (ev: MessageEvent) => {
    const data = ev.data;
    if (data && data.type === 'frame' && data.samples instanceof Int16Array) {
      onFrame(data.samples);
    }
    // type === 'ready' messages are diagnostic only; ignore here.
  };

  // The worklet must be connected to *something* that pulls samples or
  // browsers may not schedule the processor at all. Standard pattern is
  // a silent gain to destination; the gain is at 0 so nothing audible
  // leaks back to the user.
  const sink = audioContext.createGain();
  sink.gain.value = 0;
  source.connect(node);
  node.connect(sink);
  sink.connect(audioContext.destination);

  let stopped = false;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    try {
      node.port.postMessage({ type: 'flush' });
    } catch {
      // port may already be closed if the context is gone
    }
    try {
      node.port.onmessage = null;
    } catch {
      // ignore
    }
    try {
      source.disconnect();
    } catch {
      // ignore
    }
    try {
      node.disconnect();
    } catch {
      // ignore
    }
    try {
      sink.disconnect();
    } catch {
      // ignore
    }
    if (ownedContext) {
      try {
        void audioContext.close();
      } catch {
        // ignore
      }
    }
  };

  return { stop, usingFallback: false };
}

/**
 * Internal: legacy ScriptProcessorNode path. Mirrors the loop that used
 * to live inline in StreamingTest.tsx. Kept around so older Safari + a
 * (vanishingly unlikely) `audioWorklet.addModule` runtime failure does
 * not break the page; for everyone else the AudioWorklet path above is
 * the live path.
 *
 * Note: ScriptProcessorNode is deprecated in the WebAudio spec. Once
 * Safari 14.1+ is the floor everywhere (already the case for 99%+ of
 * the install base in 2026), this fallback can be deleted.
 */
function startScriptProcessorFallback(
  audioContext: AudioContext,
  source: MediaStreamAudioSourceNode,
  frameSamples: number,
  onFrame: Pcm16FrameHandler,
  ownedContext: boolean
): Pcm16Capture {
  // SP's bufferSize must be a power of two between 256 and 16384. We
  // pick 4096 to match the historical setting; it is independent of
  // the frame size emitted to the WS sender (we buffer to `frameSamples`
  // below).
  const processor = audioContext.createScriptProcessor(4096, 1, 1);
  const pending: Int16Array[] = [];
  let pendingSamples = 0;

  processor.onaudioprocess = (ev: AudioProcessingEvent) => {
    const ch = ev.inputBuffer.getChannelData(0);
    const i16 = new Int16Array(ch.length);
    for (let i = 0; i < ch.length; i++) {
      let s = ch[i];
      if (s > 1) s = 1;
      else if (s < -1) s = -1;
      i16[i] = s < 0 ? (s * 0x8000) | 0 : (s * 0x7fff) | 0;
    }
    pending.push(i16);
    pendingSamples += i16.length;

    while (pendingSamples >= frameSamples) {
      const out = new Int16Array(frameSamples);
      let written = 0;
      while (written < frameSamples && pending.length > 0) {
        const head = pending[0];
        const remaining = frameSamples - written;
        if (head.length <= remaining) {
          out.set(head, written);
          written += head.length;
          pending.shift();
        } else {
          out.set(head.subarray(0, remaining), written);
          pending[0] = head.subarray(remaining);
          written += remaining;
        }
      }
      pendingSamples -= frameSamples;
      onFrame(out);
    }
  };

  const sink = audioContext.createGain();
  sink.gain.value = 0;
  source.connect(processor);
  processor.connect(sink);
  sink.connect(audioContext.destination);

  let stopped = false;
  const stop = () => {
    if (stopped) return;
    stopped = true;
    try {
      processor.onaudioprocess = null;
    } catch {
      // ignore
    }
    try {
      source.disconnect();
    } catch {
      // ignore
    }
    try {
      processor.disconnect();
    } catch {
      // ignore
    }
    try {
      sink.disconnect();
    } catch {
      // ignore
    }
    if (ownedContext) {
      try {
        void audioContext.close();
      } catch {
        // ignore
      }
    }
  };

  return { stop, usingFallback: true };
}
