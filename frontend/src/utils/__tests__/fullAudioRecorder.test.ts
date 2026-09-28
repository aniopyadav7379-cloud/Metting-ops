/**
 * Tests for the durable chunk-streaming path in fullAudioRecorder.
 *
 * This is the FRONTEND half of the "recording survives a backend
 * restart/crash" fix. The durability mechanism is: stream each ~30s
 * MediaRecorder chunk to POST /api/recordings/sessions/{id}/audio-chunks
 * AS IT RECORDS (so the server holds chunks 0..N on disk and an
 * interruption leaves a recoverable partial), with retry/backoff on a
 * transient failure and "never halt the recording loop on a failed
 * upload" semantics.
 *
 * jsdom provides no real MediaRecorder, so we install a controllable mock
 * on globalThis (same approach as opusEncoder.test.ts's AudioEncoder mock)
 * and drive `ondataavailable` by hand. We assert on the network calls the
 * recorder makes via a mocked global `fetch`.
 *
 * What we pin:
 *   1. bufferOnly:false (the un-gate) → each chunk POSTs to /audio-chunks.
 *   2. bufferOnly:true (privacy / local-only) → NOTHING is POSTed.
 *   3. localOnly:true forces buffer-only regardless of bufferOnly.
 *   4. Transient 5xx is retried (backoff) and eventually succeeds.
 *   5. A persistent failure fires onChunkFailed and does NOT throw out of
 *      the recorder (the recording loop keeps going).
 *   6. The chunk_index increments and is sent as a form field.
 */
import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
  type MockInstance,
} from 'vitest';

import { createFullAudioRecorder } from '../fullAudioRecorder';

// ---------------------------------------------------------------------------
// Controllable MediaRecorder mock
// ---------------------------------------------------------------------------

type DataAvailableHandler = (event: { data: Blob }) => void;

class MockMediaRecorder {
  // Plain function (NOT a vi spy) so afterEach's restoreAllMocks() can't
  // strip its implementation between tests and leave pickRecordableMime
  // returning '' (which would make createFullAudioRecorder return null).
  static isTypeSupported(_mime: string): boolean {
    return true;
  }
  state: 'inactive' | 'recording' = 'inactive';
  ondataavailable: DataAvailableHandler | null = null;
  onstop: (() => void) | null = null;
  // Captured so tests can synthesize chunks on demand.
  constructor(public stream: unknown, public options?: { mimeType?: string }) {}
  start(_timeslice?: number) {
    this.state = 'recording';
  }
  stop() {
    this.state = 'inactive';
    // Real MediaRecorder fires a final dataavailable then onstop; our tests
    // drive dataavailable explicitly, so we just fire onstop.
    this.onstop?.();
  }
  requestData() {
    // No-op; tests push chunks via emit() below.
  }
}

/** Push a chunk through the recorder's dataavailable handler. */
function emit(rec: MockMediaRecorder, blob: Blob) {
  rec.ondataavailable?.({ data: blob });
}

// Hold the most-recently-constructed recorder so the test can grab it.
let lastRecorder: MockMediaRecorder | null = null;

const installedKeys: string[] = [];
let fetchSpy: MockInstance;

beforeEach(() => {
  lastRecorder = null;
  const g = globalThis as unknown as Record<string, unknown>;

  class TrackingMediaRecorder extends MockMediaRecorder {
    constructor(stream: unknown, options?: { mimeType?: string }) {
      super(stream, options);
      lastRecorder = this;
    }
  }

  // Always override so isTypeSupported resolves to our mock (jsdom may ship
  // a partial MediaRecorder stub whose isTypeSupported returns false).
  (g as Record<string, unknown>)['MediaRecorder'] = TrackingMediaRecorder;
  installedKeys.push('MediaRecorder');

  // A fresh fetch mock per test; default 200 OK with the contract shape.
  fetchSpy = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, chunk_index: 0, size: 1, chunks_received: 1 }),
    text: async () => '',
  });
  (g as Record<string, unknown>)['fetch'] = fetchSpy;
});

afterEach(() => {
  const g = globalThis as unknown as Record<string, unknown>;
  for (const k of installedKeys) delete g[k];
  installedKeys.length = 0;
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function makeStream(): MediaStream {
  // The recorder never touches the stream's methods in our mock path, so a
  // bare object cast is sufficient for jsdom.
  return {} as MediaStream;
}

/** Wait microtasks so the recorder's promise-chained upload queue drains. */
async function flushMicrotasks() {
  for (let i = 0; i < 10; i += 1) {
    await Promise.resolve();
  }
}

// ---------------------------------------------------------------------------
// 1 + 2 + 3: the un-gate / privacy gate
// ---------------------------------------------------------------------------

describe('fullAudioRecorder chunk streaming gate', () => {
  it('bufferOnly:false streams each chunk to /audio-chunks (durability un-gate)', async () => {
    const handle = createFullAudioRecorder({
      stream: makeStream(),
      sessionId: 'sess-1',
      apiUrl: 'http://api.test',
      orgSlug: 'acme',
      bufferOnly: false,
    });
    expect(handle).not.toBeNull();
    handle!.start();
    expect(lastRecorder).not.toBeNull();

    emit(lastRecorder!, new Blob(['aaa'], { type: 'audio/webm' }));
    emit(lastRecorder!, new Blob(['bbb'], { type: 'audio/webm' }));
    await flushMicrotasks();

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const [url, init] = fetchSpy.mock.calls[0]!;
    expect(String(url)).toBe(
      'http://api.test/api/recordings/sessions/sess-1/audio-chunks',
    );
    expect((init as RequestInit).method).toBe('POST');
    // chunk_index is sent as a form field, starting at 0 and incrementing.
    const form0 = (fetchSpy.mock.calls[0]![1] as RequestInit).body as FormData;
    const form1 = (fetchSpy.mock.calls[1]![1] as RequestInit).body as FormData;
    expect(form0.get('chunk_index')).toBe('0');
    expect(form1.get('chunk_index')).toBe('1');
  });

  it('bufferOnly:true (privacy invariant) POSTs nothing to the network', async () => {
    const handle = createFullAudioRecorder({
      stream: makeStream(),
      sessionId: 'sess-2',
      apiUrl: 'http://api.test',
      orgSlug: 'acme',
      bufferOnly: true,
    });
    handle!.start();
    emit(lastRecorder!, new Blob(['aaa'], { type: 'audio/webm' }));
    emit(lastRecorder!, new Blob(['bbb'], { type: 'audio/webm' }));
    await flushMicrotasks();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('localOnly:true forces buffer-only even if bufferOnly is false', async () => {
    const handle = createFullAudioRecorder({
      stream: makeStream(),
      sessionId: 'sess-3',
      apiUrl: 'http://api.test',
      orgSlug: 'acme',
      bufferOnly: false,
      localOnly: true,
    });
    handle!.start();
    emit(lastRecorder!, new Blob(['aaa'], { type: 'audio/webm' }));
    await flushMicrotasks();
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 4 + 5: retry/backoff + "never halt the recording loop"
// ---------------------------------------------------------------------------

describe('fullAudioRecorder retry/backoff', () => {
  it('retries a transient 5xx with backoff and eventually succeeds', async () => {
    vi.useFakeTimers();
    // First attempt 503, second attempt 200.
    fetchSpy
      .mockResolvedValueOnce({ ok: false, status: 503, text: async () => 'busy' })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ ok: true }),
        text: async () => '',
      });

    const uploaded: number[] = [];
    const failed: number[] = [];
    const handle = createFullAudioRecorder({
      stream: makeStream(),
      sessionId: 'sess-retry',
      apiUrl: 'http://api.test',
      orgSlug: null,
      bufferOnly: false,
      initialBackoffMs: 10,
      maxRetries: 5,
      onChunkUploaded: (i) => uploaded.push(i),
      onChunkFailed: (i) => failed.push(i),
    });
    handle!.start();
    emit(lastRecorder!, new Blob(['x'], { type: 'audio/webm' }));

    // Let the first (failing) attempt run, then advance past the backoff so
    // the retry fires.
    await vi.advanceTimersByTimeAsync(50);

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(uploaded).toEqual([0]);
    expect(failed).toEqual([]);
  });

  it('fires onChunkFailed after maxRetries and keeps recording (no throw)', async () => {
    vi.useFakeTimers();
    fetchSpy.mockResolvedValue({ ok: false, status: 500, text: async () => 'down' });

    const failed: number[] = [];
    const handle = createFullAudioRecorder({
      stream: makeStream(),
      sessionId: 'sess-fail',
      apiUrl: 'http://api.test',
      orgSlug: null,
      bufferOnly: false,
      initialBackoffMs: 1,
      maxRetries: 2,
      onChunkFailed: (i) => failed.push(i),
    });
    handle!.start();
    emit(lastRecorder!, new Blob(['x'], { type: 'audio/webm' }));

    // Drain all retries + backoffs.
    await vi.advanceTimersByTimeAsync(100);

    // 1 initial + 2 retries = 3 attempts, then give up.
    expect(fetchSpy).toHaveBeenCalledTimes(3);
    expect(failed).toEqual([0]);
    // The recorder is still recording — a dropped chunk never halts capture.
    expect(handle!.isRecording()).toBe(true);

    // And a subsequent chunk still attempts to upload (loop alive).
    fetchSpy.mockClear();
    fetchSpy.mockResolvedValue({ ok: true, status: 200, json: async () => ({}), text: async () => '' });
    emit(lastRecorder!, new Blob(['y'], { type: 'audio/webm' }));
    await vi.advanceTimersByTimeAsync(10);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it('a 4xx is permanent — failed immediately, not retried', async () => {
    fetchSpy.mockResolvedValue({ ok: false, status: 409, text: async () => 'terminal' });
    const failed: number[] = [];
    const handle = createFullAudioRecorder({
      stream: makeStream(),
      sessionId: 'sess-4xx',
      apiUrl: 'http://api.test',
      orgSlug: null,
      bufferOnly: false,
      onChunkFailed: (i) => failed.push(i),
    });
    handle!.start();
    emit(lastRecorder!, new Blob(['x'], { type: 'audio/webm' }));
    await flushMicrotasks();
    // No retry for a client error.
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(failed).toEqual([0]);
  });
});
