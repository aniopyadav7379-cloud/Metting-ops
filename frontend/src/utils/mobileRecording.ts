// Browser-side audio capture for the PWA. Server-side recording (the existing
// /api/simple/recording-sessions flow) is unreachable from a phone because the
// mic on the appliance is not the user's mic. So on mobile we fall back to
// MediaRecorder, then push the final blob through the existing chunked-upload
// pipeline (same backend processing as a manual file upload).

export interface MediaRecorderTarget {
  mimeType: string;
  extension: string;
}

export function pickRecorderTarget(): MediaRecorderTarget | null {
  if (typeof MediaRecorder === 'undefined') return null;
  const candidates: MediaRecorderTarget[] = [
    { mimeType: 'audio/webm;codecs=opus', extension: 'webm' },
    { mimeType: 'audio/webm', extension: 'webm' },
    { mimeType: 'audio/mp4;codecs=mp4a.40.2', extension: 'm4a' },
    { mimeType: 'audio/mp4', extension: 'm4a' },
    { mimeType: 'audio/aac', extension: 'aac' },
    { mimeType: 'audio/ogg;codecs=opus', extension: 'ogg' },
  ];
  for (const candidate of candidates) {
    try {
      if (MediaRecorder.isTypeSupported(candidate.mimeType)) return candidate;
    } catch {
      // isTypeSupported can throw on older Safari iterations; keep probing.
    }
  }
  return { mimeType: '', extension: 'webm' };
}

export function isLikelyTouchDevice(): boolean {
  if (typeof window === 'undefined') return false;
  if (typeof window.matchMedia === 'function') {
    if (window.matchMedia('(pointer: coarse)').matches) return true;
  }
  return /Mobi|Android|iPhone|iPad|iPod/i.test(navigator.userAgent || '');
}

export function unlockAudioContext(): void {
  // Safari refuses to deliver media data until an AudioContext has been resumed
  // from inside a user gesture. We create-and-discard one on the first tap.
  try {
    const Ctx = (window as any).AudioContext || (window as any).webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    if (ctx.state === 'suspended') ctx.resume().catch(() => undefined);
    setTimeout(() => ctx.close().catch(() => undefined), 250);
  } catch {
    // No-op: unlock is best-effort.
  }
}

export interface AudioLevelTap {
  stop: () => void;
}

export function tapAudioLevel(stream: MediaStream, onLevel: (level: number) => void): AudioLevelTap | null {
  try {
    const Ctx = (window as any).AudioContext || (window as any).webkitAudioContext;
    if (!Ctx) return null;
    const audioCtx = new Ctx();
    const source = audioCtx.createMediaStreamSource(stream);
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const buffer = new Uint8Array(analyser.frequencyBinCount);
    let frame = 0;
    let cancelled = false;
    const loop = () => {
      if (cancelled) return;
      analyser.getByteTimeDomainData(buffer);
      let peak = 0;
      for (let i = 0; i < buffer.length; i += 1) {
        const delta = Math.abs(buffer[i] - 128);
        if (delta > peak) peak = delta;
      }
      onLevel(Math.min(1, peak / 128));
      frame = requestAnimationFrame(loop);
    };
    frame = requestAnimationFrame(loop);
    return {
      stop: () => {
        cancelled = true;
        cancelAnimationFrame(frame);
        try { source.disconnect(); } catch { /* noop */ }
        audioCtx.close().catch(() => undefined);
      },
    };
  } catch {
    return null;
  }
}

export function buildRecordingFilename(extension: string, baseTitle?: string): string {
  const stem = (baseTitle || 'meeting').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'meeting';
  const date = new Date();
  const stamp = `${date.getFullYear()}${String(date.getMonth() + 1).padStart(2, '0')}${String(date.getDate()).padStart(2, '0')}-${String(date.getHours()).padStart(2, '0')}${String(date.getMinutes()).padStart(2, '0')}`;
  return `${stem}-${stamp}.${extension}`;
}
