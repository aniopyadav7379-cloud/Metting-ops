/**
 * Phase B.3 PCM16 encoder AudioWorklet.
 *
 * Replaces the deprecated ScriptProcessorNode in
 * frontend/src/pages/StreamingTest.tsx with a real audio-thread
 * processor. Runs in the AudioContext's dedicated render-thread
 * (`audio.html#AudioWorkletGlobalScope`), so it does not block the
 * main thread the way ScriptProcessor's `onaudioprocess` does.
 *
 * Contract:
 *   - Input:  single audio input, channel 0 mono, Float32, native
 *     context sample-rate (always 16000 Hz on the StreamingTest path,
 *     enforced by the AudioContext constructor).
 *   - Output: posts `Int16Array` PCM16 little-endian frames back to
 *     the main thread via `port.postMessage({type:"frame", samples})`.
 *     Frame size is configurable via the initial config message; the
 *     default matches the existing 200 ms-at-16kHz contract used by
 *     the WS sender (3200 samples / 6400 bytes).
 *
 * Message protocol on `port`:
 *   main -> worklet:
 *     { type: "config", frameSamples: <int> }  // optional, default 3200
 *   worklet -> main:
 *     { type: "frame", samples: Int16Array }   // transferable
 *     { type: "ready" }                        // posted after first
 *                                                process call so the
 *                                                main thread knows the
 *                                                node is live
 *
 * Why a separate file in /public:
 *   AudioWorklet module URLs are loaded by the browser through
 *   `audioContext.audioWorklet.addModule(url)` and the URL must be a
 *   regular HTTP-fetchable asset, NOT an ES module imported by Vite's
 *   bundle. Putting the file under `/public/audio-worklets/` means
 *   Vite copies it as-is into the build output and serves it from
 *   `/audio-worklets/pcm16-encoder.worklet.js` at runtime. Do not
 *   import this file from TypeScript code; the helper in
 *   `src/utils/audioWorkletCapture.ts` references it by URL.
 *
 * Browser support: Chrome 64+, Firefox 76+, Safari 14.1+, Edge 79+.
 * The helper module falls back to ScriptProcessorNode when
 * `audioContext.audioWorklet` is undefined (Safari < 14.1, very old
 * Firefox builds). See `audioWorkletCapture.ts`.
 */

class Pcm16EncoderProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    // 200 ms at 16 kHz = 3200 samples. Frames smaller than that get
    // batched up before posting; larger frames are split. Matches the
    // existing MIC_FRAME_SAMPLES constant in StreamingTest.tsx.
    this._frameSamples = 3200;
    // Ring of pending Int16 samples between process() calls. Each
    // process() callback delivers 128 samples (the AudioWorklet
    // render-quantum) so we accumulate until we have a full frame.
    this._buffer = new Int16Array(this._frameSamples);
    this._bufferWritten = 0;
    this._readyPosted = false;

    this.port.onmessage = (ev) => {
      const data = ev.data;
      if (data && data.type === 'config') {
        if (typeof data.frameSamples === 'number' && data.frameSamples > 0) {
          this._frameSamples = data.frameSamples | 0;
          this._buffer = new Int16Array(this._frameSamples);
          this._bufferWritten = 0;
        }
      } else if (data && data.type === 'flush') {
        // Emit whatever partial frame we have and reset. The WS sender
        // tags the trailing frame with the is_final bit; the worklet
        // itself doesn't know about the wire format.
        if (this._bufferWritten > 0) {
          const out = new Int16Array(this._bufferWritten);
          out.set(this._buffer.subarray(0, this._bufferWritten));
          this.port.postMessage(
            { type: 'frame', samples: out },
            [out.buffer]
          );
          this._bufferWritten = 0;
        }
      }
    };
  }

  process(inputs /*, outputs, parameters*/) {
    const input = inputs[0];
    if (!input || input.length === 0) {
      // No input connected yet (or upstream node disconnected). Keep
      // the processor alive; AudioWorkletProcessor.process must return
      // true to stay registered.
      return true;
    }
    const channel = input[0];
    if (!channel || channel.length === 0) {
      return true;
    }

    if (!this._readyPosted) {
      this._readyPosted = true;
      this.port.postMessage({ type: 'ready' });
    }

    // Convert Float32 [-1, 1] to Int16 [-32768, 32767] sample-by-sample.
    // Each render-quantum is 128 samples on every browser today; we
    // copy into the accumulating frame buffer and flush whenever we
    // hit `frameSamples`.
    let i = 0;
    while (i < channel.length) {
      const remaining = this._frameSamples - this._bufferWritten;
      const take = Math.min(remaining, channel.length - i);
      for (let j = 0; j < take; j++) {
        let s = channel[i + j];
        // Clamp first; Math.min/max is faster on V8 than ternary here
        // because the compiler emits a vminps/vmaxps pair.
        if (s > 1) s = 1;
        else if (s < -1) s = -1;
        // Standard asymmetric int16 conversion. Mirrors the loop the
        // ScriptProcessor was running in StreamingTest.tsx for bit-
        // exact wire compatibility.
        this._buffer[this._bufferWritten + j] =
          s < 0 ? (s * 0x8000) | 0 : (s * 0x7fff) | 0;
      }
      this._bufferWritten += take;
      i += take;

      if (this._bufferWritten >= this._frameSamples) {
        // Transfer ownership of the Int16Array buffer so the main
        // thread can reuse the underlying memory without a copy.
        const out = new Int16Array(this._frameSamples);
        out.set(this._buffer);
        this.port.postMessage(
          { type: 'frame', samples: out },
          [out.buffer]
        );
        this._bufferWritten = 0;
      }
    }

    return true;
  }
}

registerProcessor('pcm16-encoder', Pcm16EncoderProcessor);
