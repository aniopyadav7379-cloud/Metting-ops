/**
 * Phase B.2 streaming WS smoke-test page (Phase B.5 reconnect polish,
 * Phase B.3 AudioWorklet migration).
 *
 * Opens a WebSocket against `/ws/sessions/{uuid}/live`, can either:
 *   - send a dummy 1 KB PCM16-formatted binary chunk wrapped in the
 *     19-byte header (seam test, no transcription expected),
 *   - capture mic audio via getUserMedia + AudioWorklet (B.3) and
 *     stream real PCM16 frames so the meet-backend forwards them to
 *     meet-parakeet-stream-svc on midboy2.
 *
 * Prints every frame in both directions plus a separate "live
 * transcript" pane that aggregates `partial` / `final` JSON frames so
 * you can read what the model heard.
 *
 * B.3 (2026-05-26): migrated browser audio capture from the deprecated
 * `ScriptProcessorNode` (main-thread, jank-prone) to `AudioWorklet`
 * (dedicated audio render thread). Bit-exact wire-format preserved.
 * Worklet source at `frontend/public/audio-worklets/pcm16-encoder.worklet.js`;
 * helper at `frontend/src/utils/audioWorkletCapture.ts`. Falls back to
 * ScriptProcessorNode if `AudioContext.audioWorklet` is undefined
 * (Safari < 14.1; vanishingly small share of the 2026 install base).
 *
 * B.4 gating: this page reads `useTierFeatures().features.server_live`
 * and renders a friendly upgrade nudge in place of the connect UI when
 * the user is on the free tier. Pro / Enterprise / superuser-overridden
 * users see the original full UI. The backend WS endpoint also gates
 * (defense in depth): a free user that somehow reaches this page on a
 * stale bundle still gets close code 4003 from the server.
 *
 * B.5 polish: the WS uses `useReconnectingWebSocket` so an abnormal
 * close (1006 network drop, 1011 server error) triggers exponential
 * backoff reconnects (500 ms -> 30 s cap, 5 attempts) without losing
 * the session context on the page. Clean closes (1000, 1001) and
 * intentional 4xxx rejections (4001 unauth, 4003 tier_insufficient,
 * 4429 rate_limited) do NOT reconnect. Reconnect status is surfaced in
 * the log pane so we can watch the backoff ladder during a deploy.
 *
 * Wire format (canonical as of B.2 reconciliation 2026-05-22):
 *   bytes 0-3   uint32 BE  sequence_number
 *   bytes 4-11  uint64 BE  client_timestamp_us
 *   bytes 12-15 ASCII      payload_format  ("PC16" = 16 kHz mono PCM16)
 *   bytes 16-17 uint16 BE  sample_rate / 100  (160 = 16000)
 *   byte 18     uint8      flags (bit 0: is_final)
 *   bytes 19..N payload
 * See backend/api/streaming.py HEADER_STRUCT for the canonical Python
 * side and docs/phase-b-server-live-streaming.md section 4 for the
 * design rationale.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Lock } from 'lucide-react';
import { useTierFeatures } from '../hooks/useTierFeatures';
import { useReconnectingWebSocket } from '../hooks/useReconnectingWebSocket';
import {
  startPcm16Capture,
  type Pcm16Capture,
} from '../utils/audioWorkletCapture';

type MicState = 'idle' | 'requesting' | 'recording' | 'error';

const HEADER_BYTES = 19;
const DUMMY_PAYLOAD_BYTES = 1024; // ~32 ms of 16 kHz mono PCM16
const MIC_TARGET_SR = 16000;
// 200 ms of audio per WS frame at 16 kHz mono PCM16 (per design doc).
// 200ms * 16000Hz * 2 bytes = 6400 bytes per frame.
const MIC_FRAME_MS = 200;
const MIC_FRAME_SAMPLES = (MIC_TARGET_SR * MIC_FRAME_MS) / 1000;

function nowIso(): string {
  return new Date().toISOString();
}

function bytesPreview(buf: ArrayBuffer, max = 64): string {
  const view = new Uint8Array(buf, 0, Math.min(buf.byteLength, max));
  const hex = Array.from(view).map(b => b.toString(16).padStart(2, '0')).join(' ');
  return buf.byteLength > max ? `${hex} ... (+${buf.byteLength - max}B)` : hex;
}

interface SpeakerTurn {
  spk_id: number;
  spk_label?: string;
  start_ms: number;
  end_ms: number;
}

interface TranscriptEntry {
  type: 'partial' | 'final';
  sequence: number;
  text: string;
  ts: string;
  // Phase B.3 chunk D: sortformer speaker turns covering this window.
  // Empty / undefined when STREAMING_USE_SORTFORMER is off server-side or
  // when sortformer dispatch failed (the backend swallows failures so the
  // user still gets the parakeet transcript).
  speakers?: SpeakerTurn[];
  distinctSpeakerCount?: number;
}

// Speaker palette — Sortformer caps at 4 distinct speakers in v1; pick four
// visually-distinct hues that work on the page's dark background.
const SPEAKER_COLORS = ['#ef4444', '#10b981', '#3b82f6', '#f59e0b'];

function speakerColor(spkId: number): string {
  // Use modulo so any unexpected spk_id (e.g. 5+ from a future variant)
  // still renders rather than crashes.
  return SPEAKER_COLORS[((spkId % SPEAKER_COLORS.length) + SPEAKER_COLORS.length) % SPEAKER_COLORS.length];
}

interface SpeakerBadgesProps {
  speakers?: SpeakerTurn[];
}

function SpeakerBadges({ speakers }: SpeakerBadgesProps) {
  if (!speakers || speakers.length === 0) {
    return null;
  }
  // Render one badge per distinct spk_id in the window, preserving the order
  // of first appearance. Tooltip carries the start/end timestamps for the
  // first turn that spk_id appears in.
  const seen = new Map<number, SpeakerTurn>();
  for (const turn of speakers) {
    if (!seen.has(turn.spk_id)) {
      seen.set(turn.spk_id, turn);
    }
  }
  const distinct = Array.from(seen.values());
  return (
    <span style={{ display: 'inline-flex', gap: 4, marginRight: 6, verticalAlign: 'middle' }}>
      {distinct.map(turn => (
        <span
          key={turn.spk_id}
          style={{
            background: speakerColor(turn.spk_id),
            color: '#fff',
            padding: '1px 6px',
            borderRadius: 4,
            fontSize: 10,
            fontWeight: 600,
            letterSpacing: 0.2,
          }}
          title={`${turn.spk_label || `spk_${turn.spk_id}`} (first turn ${turn.start_ms}-${turn.end_ms}ms)`}
        >
          {turn.spk_label || `spk_${turn.spk_id}`}
        </span>
      ))}
    </span>
  );
}

/** Build a binary frame with the canonical 19-byte BE header followed
 *  by the supplied PCM16 LE payload. Caller is responsible for the
 *  payload being valid 16 kHz mono PCM16 (or zero-filled for smoke). */
function buildFrame(seq: number, isFinal: boolean, payload: Int16Array | Uint8Array): ArrayBuffer {
  const payloadBytes = payload instanceof Int16Array
    ? new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength)
    : payload;
  const buf = new ArrayBuffer(HEADER_BYTES + payloadBytes.byteLength);
  const view = new DataView(buf);

  view.setUint32(0, seq, /* littleEndian */ false);
  view.setBigUint64(4, BigInt(Date.now()) * 1000n, false);
  // fmt "PC16"
  const fmt = new Uint8Array(buf, 12, 4);
  fmt[0] = 0x50; fmt[1] = 0x43; fmt[2] = 0x31; fmt[3] = 0x36;
  view.setUint16(16, 160, false); // 16000 / 100
  view.setUint8(18, isFinal ? 0x01 : 0x00);

  new Uint8Array(buf, HEADER_BYTES).set(payloadBytes);
  return buf;
}

/**
 * Inner component instantiated only after the user clicks Connect.
 * The session_id + URL are captured on mount so the
 * `useReconnectingWebSocket` lifecycle contract holds (URL frozen on
 * first mount). Disconnecting unmounts this component; a new Connect
 * remounts with a fresh session id.
 */
interface ActiveStreamProps {
  url: string;
  sessionId: string;
  onLog: (line: string) => void;
  onPartialOrFinal: (entry: TranscriptEntry) => void;
  onClose: () => void;
}

function ActiveStream({ url, sessionId, onLog, onPartialOrFinal, onClose }: ActiveStreamProps) {
  // Refs (mic capture + sequence counter).
  // B.3: capture is owned by `startPcm16Capture` from
  // `utils/audioWorkletCapture`. The AudioWorklet (or ScriptProcessor
  // fallback) lives entirely behind that helper; we just hold the
  // returned handle so we can call `.stop()` on teardown.
  const seqRef = useRef(0);
  const streamRef = useRef<MediaStream | null>(null);
  const captureRef = useRef<Pcm16Capture | null>(null);

  const [micState, setMicState] = useState<MicState>('idle');
  // Disable reconnects when the user has clicked Disconnect / End.
  const userTerminatedRef = useRef<boolean>(false);

  const stopMic = useCallback(() => {
    if (captureRef.current) {
      try {
        captureRef.current.stop();
      } catch {
        // best-effort teardown; helper may already be stopped
      }
      captureRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(t => t.stop());
      streamRef.current = null;
    }
    setMicState(prev => (prev === 'error' ? prev : 'idle'));
  }, []);

  const ws = useReconnectingWebSocket({
    url,
    binaryType: 'arraybuffer',
    onOpen: () => {
      onLog(`WS open (session_id=${sessionId})`);
    },
    onMessage: (ev) => {
      if (typeof ev.data === 'string') {
        onLog(`<- ${ev.data}`);
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'partial' || msg.type === 'final') {
            onPartialOrFinal({
              type: msg.type,
              sequence: msg.sequence ?? 0,
              text: msg.text ?? '',
              ts: nowIso(),
              // Phase B.3 chunk D: speakers array from sortformer parallel
              // dispatch (empty when STREAMING_USE_SORTFORMER is off server-
              // side or when sortformer call failed for this window).
              speakers: Array.isArray(msg.speakers) ? msg.speakers : undefined,
              distinctSpeakerCount: typeof msg.sortformer_distinct_speakers === 'number'
                ? msg.sortformer_distinct_speakers
                : undefined,
            });
          }
          if (msg.type === 'server_shutdown') {
            // Server is going down for a deploy. Stop mic so we don't
            // hammer the next instance with stale frames during the
            // reconnect window; the hook will reconnect after the
            // reconnect_after_ms hint.
            stopMic();
            onLog(
              `<- server_shutdown reason=${msg.reason ?? '(none)'} ` +
              `reconnect_after_ms=${msg.reconnect_after_ms ?? '(none)'} ` +
              `(mic stopped, awaiting reconnect)`
            );
          }
        } catch {
          // not JSON; ignore for transcript aggregation
        }
      } else if (ev.data instanceof ArrayBuffer) {
        onLog(`<- binary ${ev.data.byteLength}B  ${bytesPreview(ev.data)}`);
      } else {
        onLog(`<- (unknown frame type: ${typeof ev.data})`);
      }
    },
    onClose: (ev) => {
      onLog(
        `closed code=${ev.code} reason=${ev.reason || '(none)'} wasClean=${ev.wasClean}`
      );
      // Clean stop on any close. The hook decides whether to reconnect.
      stopMic();
    },
    onError: (ev) => {
      onLog(`WS error ${ev.type}`);
    },
    onReconnect: (attempt, delayMs) => {
      onLog(`reconnecting attempt=${attempt} in ${delayMs}ms...`);
    },
    onGiveUp: () => {
      onLog('reconnect gave up after maxRetries; staying disconnected');
      onClose();
    },
  });

  const readyStateLabel = (() => {
    switch (ws.readyState) {
      case 0: return 'connecting';
      case 1: return 'open';
      case 2: return 'closing';
      case 3: return ws.gaveUp ? 'gave_up' : (ws.retries > 0 ? 'reconnecting' : 'closed');
      default: return `state_${ws.readyState}`;
    }
  })();

  const connected = ws.readyState === 1;
  const micRecording = micState === 'recording';

  const sendDummyChunk = () => {
    if (!connected) {
      onLog('cannot send: ws not open');
      return;
    }
    seqRef.current += 1;
    // Payload is left as zeros, this is a seam test, not an audio test.
    const buf = buildFrame(seqRef.current, false, new Uint8Array(DUMMY_PAYLOAD_BYTES));
    const ok = ws.send(buf);
    if (ok) {
      onLog(`-> binary seq=${seqRef.current} ${buf.byteLength}B (header ${HEADER_BYTES} + payload ${DUMMY_PAYLOAD_BYTES})`);
    } else {
      onLog('-> binary send failed (ws not open)');
    }
  };

  const startMic = async () => {
    if (!connected) {
      onLog('cannot start mic: ws not open');
      return;
    }
    if (micState === 'recording' || micState === 'requesting') {
      onLog('mic already starting/recording');
      return;
    }
    try {
      setMicState('requesting');
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: MIC_TARGET_SR,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      streamRef.current = stream;

      // B.3: capture moves to AudioWorklet. The helper picks the
      // AudioWorklet path on modern browsers (Chrome 64+, Firefox 76+,
      // Safari 14.1+) and silently falls back to ScriptProcessorNode on
      // older Safari. Wire format is preserved bit-for-bit so the WS
      // server side does not need a corresponding change.
      const capture = await startPcm16Capture(
        stream,
        (frame) => {
          // `frame` is an Int16Array of exactly MIC_FRAME_SAMPLES samples
          // (3200 = 200 ms at 16 kHz). One WS binary frame per callback.
          seqRef.current += 1;
          const buf = buildFrame(seqRef.current, false, frame);
          // Best-effort: only ship if the socket is OPEN. While the
          // hook is reconnecting, we drop frames. The PCM ring buffer
          // on the server is for the live transcript window only, so
          // dropping a few seconds during a reconnect is the right
          // behavior.
          ws.send(buf);
        },
        {
          sampleRate: MIC_TARGET_SR,
          frameSamples: MIC_FRAME_SAMPLES,
        }
      );
      captureRef.current = capture;

      setMicState('recording');
      onLog(
        `mic started (${MIC_TARGET_SR} Hz mono, ~${MIC_FRAME_MS} ms frames, ` +
        `${capture.usingFallback ? 'ScriptProcessor fallback' : 'AudioWorklet'})`
      );
    } catch (err) {
      console.error(err);
      onLog(`mic error: ${(err as Error).message}`);
      setMicState('error');
      stopMic();
    }
  };

  const sendFlush = () => {
    if (!ws.send(JSON.stringify({ type: 'flush' }))) {
      onLog('cannot send: ws not open');
      return;
    }
    onLog('-> {"type":"flush"}');
  };

  const sendPing = () => {
    if (!ws.send(JSON.stringify({ type: 'ping' }))) {
      onLog('cannot send: ws not open');
      return;
    }
    onLog('-> {"type":"ping"}');
  };

  const sendEnd = () => {
    userTerminatedRef.current = true;
    stopMic();
    if (!ws.send(JSON.stringify({ type: 'end' }))) {
      onLog('cannot send: ws not open');
      onClose();
      return;
    }
    onLog('-> {"type":"end"}');
    // The server will reply with {type:"closed"} + close 1000. The hook
    // sees the clean close and won't reconnect. We still tear down the
    // outer state here so a slow server doesn't keep the UI in limbo.
    setTimeout(() => onClose(), 500);
  };

  const disconnect = () => {
    userTerminatedRef.current = true;
    stopMic();
    ws.close(1000, 'client disconnect');
    onLog('-> client-initiated close 1000');
    onClose();
  };

  // Tear down mic if we unmount.
  useEffect(() => {
    return () => {
      stopMic();
    };
  }, [stopMic]);

  return (
    <>
      <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
        <button onClick={sendDummyChunk} disabled={!connected}>
          Send dummy chunk
        </button>
        <button onClick={startMic} disabled={!connected || micRecording}>
          Start mic
        </button>
        <button onClick={stopMic} disabled={!micRecording}>
          Stop mic
        </button>
        <button onClick={sendFlush} disabled={!connected}>
          Send {'{"type":"flush"}'}
        </button>
        <button onClick={sendPing} disabled={!connected}>
          Send {'{"type":"ping"}'}
        </button>
        <button onClick={sendEnd} disabled={!connected}>
          Send {'{"type":"end"}'}
        </button>
        <button onClick={disconnect}>
          Disconnect
        </button>
      </div>

      <div style={{ marginBottom: 8, fontSize: 13, opacity: 0.8 }}>
        ws: <strong>{readyStateLabel}</strong>
        {' · '}
        mic: <strong>{micState}</strong>
        {' · '}
        session_id: <code>{sessionId}</code>
        {' · '}
        seq: <code>{seqRef.current}</code>
        {ws.retries > 0 && (
          <>
            {' · '}
            retries: <strong style={{ color: ws.gaveUp ? '#ef4444' : '#facc15' }}>
              {ws.retries}{ws.gaveUp ? ' (gave up)' : ''}
            </strong>
          </>
        )}
      </div>
    </>
  );
}

export default function StreamingTest() {
  const { tier, features } = useTierFeatures();
  const [log, setLog] = useState<string[]>([]);
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  // Active session info; when null, the connect UI shows. Setting this
  // mounts ActiveStream which owns the WS lifecycle.
  const [active, setActive] = useState<{ url: string; sessionId: string } | null>(null);

  const append = useCallback((line: string) => {
    setLog(prev => {
      const next = [...prev, `[${nowIso()}] ${line}`];
      return next.length > 500 ? next.slice(next.length - 500) : next;
    });
  }, []);

  const handlePartialOrFinal = useCallback((entry: TranscriptEntry) => {
    setTranscript(prev => [...prev, entry].slice(-200));
  }, []);

  const handleClose = useCallback(() => {
    setActive(null);
  }, []);

  const connect = () => {
    if (active) {
      append('already connected; disconnect first');
      return;
    }
    const sessionId = (globalThis.crypto?.randomUUID?.() ?? `b2-${Date.now().toString(36)}`);
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws/sessions/${sessionId}/live`;
    setTranscript([]);
    append(`connecting -> ${url}`);
    setActive({ url, sessionId });
  };

  const clearLog = () => setLog([]);
  const clearTranscript = () => setTranscript([]);

  // B.4 tier gate. Render an upgrade nudge for users without the
  // `server_live` capability (free tier). Defense in depth: the backend
  // WS endpoint also rejects free-tier connects with close code 4003,
  // so this UI nudge isn't load-bearing for the security model, it's
  // a friendlier experience than "connect, instant disconnect".
  if (!features.server_live) {
    return (
      <div style={{ padding: 24, color: '#e7e7e7', maxWidth: 720 }}>
        <div
          style={{
            background: '#0a0a0a',
            border: '1px solid #1f1f1f',
            borderRadius: 8,
            padding: 32,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'flex-start',
            gap: 14,
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: 56,
              height: 56,
              borderRadius: '50%',
              background: '#1a1a1a',
              border: '1px solid #2a2a2a',
            }}
            aria-hidden
          >
            <Lock size={28} color="#facc15" />
          </div>
          <h1 style={{ margin: 0, fontSize: 22 }}>
            Server-live streaming is a Pro feature
          </h1>
          <p style={{ margin: 0, opacity: 0.85, lineHeight: 1.55 }}>
            Your current tier is <strong>{tier}</strong>. Live streaming
            requires Pro or Enterprise.
          </p>
          <p style={{ margin: 0, opacity: 0.7, lineHeight: 1.55 }}>
            Once you upgrade, this page will let you test live
            transcription against the server-side Parakeet 0.6B v3
            model running on midboy2.
          </p>
          <div style={{ display: 'flex', gap: 10, marginTop: 6 }}>
            <button
              disabled
              title="Server-live streaming requires Pro or Enterprise"
              style={{
                padding: '8px 16px',
                borderRadius: 6,
                border: '1px solid #2a2a2a',
                background: '#141414',
                color: '#666',
                cursor: 'not-allowed',
              }}
            >
              Connect
            </button>
            <a
              href="mailto:hello@magicunicorn.tech?subject=Pro%20tier%20for%20Meeting-Ops%20live%20streaming"
              style={{
                padding: '8px 16px',
                borderRadius: 6,
                border: '1px solid #facc15',
                background: 'transparent',
                color: '#facc15',
                textDecoration: 'none',
                fontSize: 14,
              }}
            >
              Contact us about Pro
            </a>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div style={{ padding: 24, color: '#e7e7e7', maxWidth: 1080 }}>
      <h1 style={{ marginTop: 0 }}>Streaming Test (Phase B.2 / B.5)</h1>
      <p style={{ opacity: 0.7, marginTop: 4 }}>
        Opens a WebSocket against{' '}
        <code>/ws/sessions/&lt;uuid&gt;/live</code> and either streams a
        dummy zero-filled chunk (seam test) or real microphone audio
        (real STT). Backend forwards windowed audio to{' '}
        <code>meet-parakeet-stream-svc</code> on midboy2 every ~2.5 s
        and emits <code>partial</code> / <code>final</code> JSON frames
        back. B.5 adds exponential-backoff reconnect on abnormal closes
        via <code>useReconnectingWebSocket</code>. True NeMo native
        streaming lands in B.3; see{' '}
        <code>docs/phase-b-server-live-streaming.md</code>.
      </p>

      {/* Connect button is only visible when no active session. */}
      {!active && (
        <div style={{ display: 'flex', gap: 8, marginBottom: 16, flexWrap: 'wrap' }}>
          <button onClick={connect}>
            Connect
          </button>
        </div>
      )}

      {active && (
        <ActiveStream
          key={active.sessionId}
          url={active.url}
          sessionId={active.sessionId}
          onLog={append}
          onPartialOrFinal={handlePartialOrFinal}
          onClose={handleClose}
        />
      )}

      {/* Live transcript pane, populated from partial/final JSON frames. */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 6 }}>
          <strong style={{ fontSize: 14 }}>Live transcript</strong>
          <button onClick={clearTranscript} style={{ fontSize: 12 }}>
            Clear
          </button>
          <span style={{ fontSize: 12, opacity: 0.6 }}>
            {transcript.length} entries
          </span>
        </div>
        <div
          style={{
            background: '#080808',
            padding: 12,
            borderRadius: 6,
            fontSize: 14,
            maxHeight: 280,
            overflow: 'auto',
            border: '1px solid #1f1f1f',
            lineHeight: 1.5,
          }}
        >
          {transcript.length === 0 ? (
            <span style={{ opacity: 0.4 }}>
              (no partials yet, connect, start mic, and speak)
            </span>
          ) : (
            transcript.map((e, i) => (
              <div
                key={`${e.sequence}-${i}`}
                style={{
                  opacity: e.type === 'final' ? 1 : 0.72,
                  fontWeight: e.type === 'final' ? 500 : 400,
                  borderLeft: e.type === 'final' ? '2px solid #4caf50' : '2px solid #1976d2',
                  paddingLeft: 8,
                  marginBottom: 4,
                }}
                title={`${e.type} seq=${e.sequence} ${e.ts}`}
              >
                <SpeakerBadges speakers={e.speakers} />
                {e.text || <em style={{ opacity: 0.5 }}>(empty)</em>}
              </div>
            ))
          )}
        </div>
      </div>

      <div style={{ marginBottom: 8, fontSize: 12, opacity: 0.6 }}>
        Raw WS traffic ({log.length} lines, capped at 500):
        <button onClick={clearLog} style={{ marginLeft: 8, fontSize: 12 }}>
          Clear log
        </button>
      </div>

      <pre
        style={{
          background: '#0a0a0a',
          padding: 12,
          borderRadius: 6,
          fontSize: 12,
          maxHeight: 400,
          overflow: 'auto',
          border: '1px solid #1f1f1f',
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
        }}
      >
        {log.length === 0 ? '(no traffic yet, click Connect)' : log.join('\n')}
      </pre>
    </div>
  );
}
