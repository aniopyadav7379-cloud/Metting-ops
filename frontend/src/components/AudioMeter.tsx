import { useEffect, useRef } from 'react';

/**
 * Live VU meter rendered as a horizontal bar. Pulls a `MediaStream`,
 * builds its own AudioContext + AnalyserNode, and animates via
 * requestAnimationFrame.
 *
 * Decoupled from the VAD pipeline by design: the VAD engine has its own
 * AudioWorklet inside `@ricky0123/vad-web`, and reusing those internals
 * would chain UI state to model frame timing. A separate analyser is
 * cheap (a few microseconds per frame).
 *
 * Two render modes:
 *   - With `stream`: live levels from getUserMedia.
 *   - With `levelGetter`: external function returning [0, 1]. Useful when
 *     the parent already owns a stream we can sample (e.g. mic-test
 *     playback wants to show the *recorded* waveform).
 *
 * Cleanup is critical: the AudioContext + sourceNode keep the mic "in
 * use" indefinitely if not closed. Effect teardown stops the rAF, closes
 * the AudioContext, and DOES NOT stop the underlying stream tracks (the
 * parent owns the stream lifecycle).
 */

export interface AudioMeterProps {
  stream?: MediaStream | null;
  levelGetter?: () => number;
  /** ARIA label for the meter region. */
  ariaLabel?: string;
  /** Show a numeric percentage to the right. */
  showNumeric?: boolean;
  /** Compact mode shrinks bar height. Default false (24px), compact = 8px. */
  compact?: boolean;
  /** Optional className for the outer wrapper. */
  className?: string;
}

const FFT_SIZE = 2048;

export default function AudioMeter({
  stream,
  levelGetter,
  ariaLabel = 'Microphone level',
  showNumeric = false,
  compact = false,
  className = '',
}: AudioMeterProps) {
  const barRef = useRef<HTMLDivElement | null>(null);
  const numericRef = useRef<HTMLSpanElement | null>(null);
  const rafRef = useRef<number | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const sourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const dataRef = useRef<Float32Array | null>(null);

  useEffect(() => {
    // Mode 1: external level getter — just animate with the supplied fn.
    if (levelGetter) {
      const tick = () => {
        const lvl = Math.max(0, Math.min(1, levelGetter()));
        applyLevel(barRef.current, numericRef.current, lvl);
        rafRef.current = requestAnimationFrame(tick);
      };
      rafRef.current = requestAnimationFrame(tick);
      return () => {
        if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      };
    }

    // Mode 2: stream-driven. Build our own analyser graph.
    if (!stream) {
      // No stream and no getter — render an empty (zero-level) bar.
      applyLevel(barRef.current, numericRef.current, 0);
      return;
    }

    let cancelled = false;
    (async () => {
      try {
        // Some browsers (Safari) require the AudioContext to be created
        // *after* a user gesture. By the time we have a stream there's
        // already been one, but if context creation fails we silently
        // degrade to a zero bar rather than crashing the UI.
        const AudioCtxCtor =
          (window as Window & {
            AudioContext: typeof AudioContext;
            webkitAudioContext?: typeof AudioContext;
          }).AudioContext ||
          (window as Window & {
            webkitAudioContext?: typeof AudioContext;
          }).webkitAudioContext;
        if (!AudioCtxCtor) return;

        const ctx = new AudioCtxCtor();
        if (cancelled) {
          await ctx.close().catch(() => undefined);
          return;
        }
        ctxRef.current = ctx;

        const analyser = ctx.createAnalyser();
        analyser.fftSize = FFT_SIZE;
        analyser.smoothingTimeConstant = 0.6;
        analyserRef.current = analyser;

        const source = ctx.createMediaStreamSource(stream);
        source.connect(analyser);
        sourceRef.current = source;

        const data = new Float32Array(analyser.fftSize);
        dataRef.current = data;

        const tick = () => {
          const a = analyserRef.current;
          const d = dataRef.current;
          if (!a || !d) return;
          a.getFloatTimeDomainData(d);
          // RMS → 0..1 scale.
          let sum = 0;
          for (let i = 0; i < d.length; i += 1) sum += d[i] * d[i];
          const rms = Math.sqrt(sum / d.length);
          // Quiet floor ~0.005, loud ~0.3. Map to 0..1 with a soft
          // logarithmic curve so quiet speech is visible without
          // saturating on louder peaks.
          const norm = Math.min(1, Math.max(0, (Math.log10(rms + 1e-6) + 3) / 2.5));
          applyLevel(barRef.current, numericRef.current, norm);
          rafRef.current = requestAnimationFrame(tick);
        };

        rafRef.current = requestAnimationFrame(tick);
      } catch (e) {
        // Silently degrade. Console-warn for diagnostics.
        // eslint-disable-next-line no-console
        console.warn('[AudioMeter] Failed to attach analyser:', e);
      }
    })();

    return () => {
      cancelled = true;
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      try {
        sourceRef.current?.disconnect();
      } catch {
        // ignore
      }
      sourceRef.current = null;
      analyserRef.current = null;
      dataRef.current = null;
      const ctx = ctxRef.current;
      ctxRef.current = null;
      if (ctx && ctx.state !== 'closed') {
        ctx.close().catch(() => undefined);
      }
      applyLevel(barRef.current, numericRef.current, 0);
    };
  }, [stream, levelGetter]);

  const height = compact ? 8 : 24;

  return (
    <div
      className={`flex items-center gap-2 ${className}`}
      role="region"
      aria-label={ariaLabel}
    >
      <div
        className="relative flex-1 overflow-hidden rounded-full bg-zinc-800/80"
        style={{ height }}
      >
        <div
          ref={barRef}
          className="absolute inset-y-0 left-0 bg-gradient-to-r from-emerald-500 via-emerald-400 to-amber-300 transition-[width] duration-75"
          style={{ width: '0%' }}
        />
      </div>
      {showNumeric && (
        <span
          ref={numericRef}
          className="w-10 text-right font-mono text-xs tabular-nums text-zinc-400"
        >
          0%
        </span>
      )}
    </div>
  );
}

function applyLevel(
  bar: HTMLDivElement | null,
  numeric: HTMLSpanElement | null,
  level: number,
): void {
  if (bar) bar.style.width = `${(level * 100).toFixed(1)}%`;
  if (numeric) numeric.textContent = `${Math.round(level * 100)}%`;
}
