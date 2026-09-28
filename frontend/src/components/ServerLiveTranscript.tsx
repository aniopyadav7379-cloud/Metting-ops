/**
 * ServerLiveTranscript — Phase B.2/B.3 server-live transcript pane with
 * Sortformer speaker labels. Drop-in for any page that has an active
 * recording session and wants live transcript + speaker badges.
 *
 * Opens its own WebSocket to /ws/sessions/{sessionId}/live and runs its
 * own mic capture via the AudioWorklet helper. Coexists with whatever
 * other audio pipelines are running on the page — browsers happily share
 * one OS-level mic across multiple getUserMedia() calls.
 *
 * Designed for the Record page (LiveRecording.tsx) but reusable
 * anywhere. The admin-only /streaming-test page keeps its own UI for
 * the seam-test buttons; this component is the production view.
 *
 * Tier-gating happens at the call site — only render this for users
 * with `hasFeature('server_live')` true. Free-tier users see a
 * different UI path entirely.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { Users } from 'lucide-react';
import { appendWsToken } from '../config';
import { startPcm16Capture, type Pcm16Capture } from '../utils/audioWorkletCapture';

// Mirror the 19-byte BE header layout from StreamingTest.tsx + the
// streaming.py module docstring. Kept inline here so this component has
// no dependency on the admin page.
const HEADER_BYTES = 19;
const MIC_TARGET_SR = 16000;
const MIC_FRAME_SAMPLES = 3200; // 200 ms at 16 kHz

function buildFrame(seq: number, isFinal: boolean, payload: Int16Array): ArrayBuffer {
  const payloadBytes = new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength);
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

// ---- Speaker badge rendering (kept in sync with StreamingTest.tsx) ----

interface SpeakerTurn {
  spk_id: number;
  spk_label?: string;
  start_ms: number;
  end_ms: number;
}

const SPEAKER_COLORS = ['#ef4444', '#10b981', '#3b82f6', '#f59e0b'];

function speakerColor(spkId: number): string {
  return SPEAKER_COLORS[
    ((spkId % SPEAKER_COLORS.length) + SPEAKER_COLORS.length) % SPEAKER_COLORS.length
  ];
}

function SpeakerBadges({ speakers }: { speakers?: SpeakerTurn[] }) {
  if (!speakers || speakers.length === 0) return null;
  const seen = new Map<number, SpeakerTurn>();
  for (const turn of speakers) {
    if (!seen.has(turn.spk_id)) seen.set(turn.spk_id, turn);
  }
  return (
    <span style={{ display: 'inline-flex', gap: 4, marginRight: 6, verticalAlign: 'middle' }}>
      {Array.from(seen.values()).map(turn => (
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

// v2.1.0: per-word utterance view. Renders accumulated utterances as
// paragraph blocks (speaker badges + words + EOU chip + draft suffix on
// the trailing active utterance). React re-renders on every state
// change but the DOM diff is small because most words don't change
// across frames — only the trailing draft + occasional new finalized
// words land.
interface UtteranceViewProps {
  utterances: Utterance[];
  currentDraft: string;
}

function UtteranceView({ utterances, currentDraft }: UtteranceViewProps) {
  return (
    <>
      {utterances.map((u, idx) => {
        const isActive = idx === utterances.length - 1 && !u.eouSealed;
        return (
          <div
            key={`utt-${u.startSequence}-${idx}`}
            style={{
              opacity: u.eouSealed ? 1 : 0.92,
              borderLeft: u.eouSealed
                ? '2px solid #4caf50'
                : '2px solid #1976d2',
              paddingLeft: 8,
              marginBottom: 6,
            }}
          >
            <SpeakerBadges speakers={u.speakers} />
            {u.tokens.map((t, i) => (
              <span key={`w-${i}-${t.start}`} title={`${t.start.toFixed(2)}s-${t.end.toFixed(2)}s${typeof t.confidence === 'number' ? ` conf=${t.confidence.toFixed(2)}` : ''}`}>
                {i > 0 ? ' ' : ''}
                {t.word}
              </span>
            ))}
            {isActive && currentDraft && (
              <span style={{ opacity: 0.45, fontStyle: 'italic', marginLeft: 4 }}>
                {' '}{currentDraft}
              </span>
            )}
            {u.eouSealed && (
              <span
                style={{
                  marginLeft: 6,
                  padding: '1px 5px',
                  borderRadius: 3,
                  background: '#52525b',
                  color: '#d4d4d8',
                  fontSize: 10,
                  fontWeight: 600,
                  letterSpacing: 0.3,
                }}
                title="End of utterance detected by streaming model"
              >
                EOU
              </span>
            )}
          </div>
        );
      })}
    </>
  );
}

// ---- Component ----

interface FinalizedToken {
  word: string;
  start: number;
  end: number;
  confidence?: number;
}

interface TranscriptEntry {
  type: 'partial' | 'final';
  sequence: number;
  text: string;
  speakers?: SpeakerTurn[];
  // v2.0.0: per-word finalized tokens (delta — only the words newly
  // promoted from draft → finalized in THIS frame). Empty arrays when
  // STREAMING_USE_V2_PARAKEET is off server-side.
  tokensFinalized?: FinalizedToken[];
  textDraft?: string;
  eouDetected?: boolean;
}

interface ServerLiveTranscriptProps {
  /** Session id from the parent page. Used as the WS path segment. */
  sessionId: string;
  /** When false, the WS + mic are kept torn down. Parent typically passes
   *  `isRecording && tier-allows-this`. */
  enabled: boolean;
}

type ConnState = 'idle' | 'connecting' | 'open' | 'closed' | 'error';

// v2.1.0: utterance-grouped accumulation for per-word streaming render.
// Each Utterance is a paragraph of finalized words, separated by EOU
// boundaries. Within an utterance, words arrive incrementally as the
// model commits them — closer to how a real-time captioner displays.
interface Utterance {
  tokens: FinalizedToken[];
  speakers?: SpeakerTurn[];
  eouSealed: boolean;
  // First sequence number that contributed to this utterance — used as
  // React key + for ordering.
  startSequence: number;
}

export default function ServerLiveTranscript({ sessionId, enabled }: ServerLiveTranscriptProps) {
  const [conn, setConn] = useState<ConnState>('idle');
  // Legacy per-partial-frame view, kept for back-compat with the v1
  // endpoint (no tokens_finalized).
  const [transcript, setTranscript] = useState<TranscriptEntry[]>([]);
  // v2.1.0: utterance-grouped per-word view. Activates as soon as any
  // partial frame carries tokens_finalized.
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  // Current in-flight draft suffix (replaced, not appended). Shown
  // faint at the end of the active utterance.
  const [currentDraft, setCurrentDraft] = useState<string>('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const seqRef = useRef(0);
  const streamRef = useRef<MediaStream | null>(null);
  const captureRef = useRef<Pcm16Capture | null>(null);
  const userTerminatedRef = useRef(false);

  // Auto-scroll (stick-to-bottom). We tail the transcript to the latest
  // content as it streams, but pause the moment the user scrolls up to
  // read back, and resume once they return to the bottom. `atBottomRef`
  // is the live intent flag (avoids a state round-trip per scroll event);
  // `stickToBottom` mirrors it into state only to toggle the small
  // "follow paused" affordance.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  const [stickToBottom, setStickToBottom] = useState(true);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    // 24px slack so "close enough to the bottom" still counts as docked.
    const docked = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    atBottomRef.current = docked;
    setStickToBottom((prev) => (prev === docked ? prev : docked));
  }, []);

  // Compose the WS URL. Same pattern StreamingTest.tsx uses.
  const wsUrl = (() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Carry the JWT as ?token= — the browser WS API can't set headers, and the
    // server-live socket is token-authed (streaming.py authenticate_ws). Without
    // this the upgrade is rejected 4001 for browser clients / on the native-OIDC
    // prod path (no oauth2-proxy injecting forward-auth headers).
    return appendWsToken(
      `${proto}//${window.location.host}/ws/sessions/${encodeURIComponent(sessionId)}/live`,
    );
  })();

  const teardown = useCallback(() => {
    if (captureRef.current) {
      try { captureRef.current.stop(); } catch { /* best effort */ }
      captureRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(t => t.stop());
      streamRef.current = null;
    }
    if (wsRef.current) {
      try { wsRef.current.close(1000, 'component teardown'); } catch { /* best effort */ }
      wsRef.current = null;
    }
    seqRef.current = 0;
  }, []);

  useEffect(() => {
    if (!enabled || !sessionId) {
      userTerminatedRef.current = true;
      teardown();
      setConn('idle');
      setTranscript([]);
      setUtterances([]);
      setCurrentDraft('');
      setErrorMsg(null);
      return;
    }
    userTerminatedRef.current = false;
    setConn('connecting');
    setErrorMsg(null);

    const ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;

    ws.onopen = () => {
      setConn('open');
      // Once WS is open, attach mic and start streaming binary frames.
      (async () => {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({
            audio: { channelCount: 1, sampleRate: MIC_TARGET_SR },
          });
          if (userTerminatedRef.current) {
            stream.getTracks().forEach(t => t.stop());
            return;
          }
          streamRef.current = stream;
          const capture = await startPcm16Capture(
            stream,
            (frame) => {
              if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                seqRef.current += 1;
                const buf = buildFrame(seqRef.current, false, frame);
                wsRef.current.send(buf);
              }
            },
            { sampleRate: MIC_TARGET_SR, frameSamples: MIC_FRAME_SAMPLES },
          );
          if (userTerminatedRef.current) {
            try { capture.stop(); } catch { /* best effort */ }
            return;
          }
          captureRef.current = capture;
        } catch (err: unknown) {
          const msg = err instanceof Error ? err.message : String(err);
          setErrorMsg(`Mic access denied: ${msg}`);
          setConn('error');
        }
      })();
    };

    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') return;
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'partial' || msg.type === 'final') {
          const tokensFinalized: FinalizedToken[] | undefined =
            Array.isArray(msg.tokens_finalized)
              ? (msg.tokens_finalized as FinalizedToken[])
              : undefined;
          const speakersThisFrame: SpeakerTurn[] | undefined =
            Array.isArray(msg.speakers) ? msg.speakers : undefined;
          const textDraft = typeof msg.text_draft === 'string' ? msg.text_draft : '';
          const eouDetected = Boolean(msg.eou_detected);
          const entry: TranscriptEntry = {
            type: msg.type,
            sequence: msg.sequence ?? 0,
            text: (msg.text ?? '').trim(),
            speakers: speakersThisFrame,
            tokensFinalized,
            textDraft: textDraft || undefined,
            eouDetected,
          };
          // Allow empty text when an EOU marker fires alone — the chip
          // alone is informative even without new words.
          if (!entry.text && !entry.eouDetected && !tokensFinalized?.length) return;

          // v2.1.0: utterance-grouped accumulator. Appends newly
          // finalized tokens to the trailing utterance; seals it (starts
          // a new one) on EOU. Only activates when tokens_finalized is
          // non-empty in the incoming frame — otherwise the legacy
          // transcript view below handles rendering.
          if (tokensFinalized && tokensFinalized.length > 0) {
            setUtterances(prev => {
              const next = [...prev];
              let tail = next[next.length - 1];
              if (!tail || tail.eouSealed) {
                tail = {
                  tokens: [],
                  speakers: speakersThisFrame,
                  eouSealed: false,
                  startSequence: entry.sequence,
                };
                next.push(tail);
              }
              tail.tokens = [...tail.tokens, ...tokensFinalized];
              // Latest speakers win — sortformer's view of who is in
              // this window. Past utterances keep the speakers from when
              // they were sealed.
              if (speakersThisFrame && speakersThisFrame.length > 0) {
                tail.speakers = speakersThisFrame;
              }
              if (eouDetected) {
                tail.eouSealed = true;
              }
              // Cap at 50 utterances to keep DOM manageable on long
              // sessions. Older utterances drop off the top.
              return next.length > 50 ? next.slice(-50) : next;
            });
            setCurrentDraft(textDraft || '');
          } else if (eouDetected && tokensFinalized === undefined) {
            // EOU-only frame on the v1 path: seal whatever the last
            // utterance was (if any).
            setUtterances(prev => {
              if (prev.length === 0) return prev;
              const tail = prev[prev.length - 1];
              if (tail.eouSealed) return prev;
              return [...prev.slice(0, -1), { ...tail, eouSealed: true }];
            });
            setCurrentDraft('');
          }

          // Always append to legacy transcript view as a back-compat
          // fallback. The render branch decides which view to display.
          setTranscript(prev => {
            const next = [...prev, entry];
            return next.length > 200 ? next.slice(-200) : next;
          });
        } else if (msg.type === 'error') {
          setErrorMsg(`${msg.reason ?? 'error'}: ${msg.detail ?? ''}`);
        }
      } catch {
        // Ignore non-JSON text frames.
      }
    };

    ws.onerror = () => {
      setErrorMsg('WebSocket error');
      setConn('error');
    };

    ws.onclose = () => {
      setConn('closed');
      if (!userTerminatedRef.current) {
        // Mic capture continues to be live in case the parent re-enables.
        // The teardown effect cleanup will handle real shutdown.
      }
    };

    return () => {
      userTerminatedRef.current = true;
      teardown();
    };
  }, [enabled, sessionId, wsUrl, teardown]);

  // Tail-follow: scroll the transcript container to the bottom whenever
  // its content grows, unless the user has scrolled up. Depends on every
  // signal that adds height: utterance count, the trailing draft, and the
  // legacy transcript entry count.
  useEffect(() => {
    if (!atBottomRef.current) return;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [utterances, currentDraft, transcript]);

  // Distinct live-speaker count from Sortformer turns. This stream is the
  // ONLY live source that carries per-segment speaker labels, so the count
  // is meaningful here (the parent's browser transcript has none). We union
  // spk_ids across both the utterance accumulator and the legacy entry
  // list so the count holds steady regardless of which render path is live.
  const speakerCount = (() => {
    const ids = new Set<number>();
    for (const u of utterances) {
      for (const s of u.speakers ?? []) ids.add(s.spk_id);
    }
    for (const e of transcript) {
      for (const s of e.speakers ?? []) ids.add(s.spk_id);
    }
    return ids.size;
  })();

  if (!enabled) return null;

  return (
    <div
      style={{
        marginTop: 16,
        marginBottom: 16,
        background: '#080808',
        padding: 12,
        borderRadius: 6,
        border: '1px solid #1f1f1f',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 6, flexWrap: 'wrap' }}>
        <strong style={{ fontSize: 14 }}>Server live transcript</strong>
        <span style={{ fontSize: 11, opacity: 0.5 }}>
          (Phase B.2 — Parakeet streaming + Sortformer speakers)
        </span>
        {/* VAD / listening pulse — the mic is actively streaming PCM frames
            to the server only while the WS is open, so an open connection
            is a true "we are capturing + listening" signal. A subtle
            animated dot rather than a banner. */}
        {conn === 'open' && (
          <span
            title="Listening — streaming audio to the server"
            style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 11, color: '#a7f3d0' }}
          >
            <span style={{ position: 'relative', width: 8, height: 8, display: 'inline-block' }}>
              <span className="animate-ping" style={{ position: 'absolute', inset: 0, borderRadius: 9999, background: '#34d399', opacity: 0.75 }} />
              <span style={{ position: 'absolute', inset: 0, borderRadius: 9999, background: '#10b981' }} />
            </span>
            Listening
          </span>
        )}
        {/* Live distinct-speaker count (live diarization only). Sortformer
            populates this; renders only once at least one speaker turn has
            arrived so we never show "0 speakers" pre-roll. */}
        {speakerCount > 0 && (
          <span
            title="Distinct speakers detected by live diarization"
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              fontSize: 11,
              padding: '1px 8px',
              borderRadius: 9999,
              background: 'rgba(217, 70, 239, 0.12)',
              border: '1px solid rgba(217, 70, 239, 0.35)',
              color: '#f0abfc',
            }}
          >
            <Users size={12} strokeWidth={2} />
            {speakerCount} {speakerCount === 1 ? 'speaker' : 'speakers'}
          </span>
        )}
        <span
          style={{
            fontSize: 11,
            padding: '1px 6px',
            borderRadius: 4,
            background:
              conn === 'open' ? '#10b981' :
              conn === 'connecting' ? '#f59e0b' :
              conn === 'error' ? '#ef4444' :
              '#3f3f46',
            color: '#fff',
          }}
        >
          {conn}
        </span>
        <span style={{ fontSize: 11, opacity: 0.5 }}>
          {transcript.length} entries
        </span>
        {/* Tail-follow paused affordance — appears only when the user has
            scrolled up off the bottom. One click re-docks + resumes. */}
        {!stickToBottom && (
          <button
            type="button"
            onClick={() => {
              const el = scrollRef.current;
              if (el) el.scrollTop = el.scrollHeight;
              atBottomRef.current = true;
              setStickToBottom(true);
            }}
            style={{
              fontSize: 11,
              padding: '1px 8px',
              borderRadius: 9999,
              background: 'rgba(99, 102, 241, 0.15)',
              border: '1px solid rgba(99, 102, 241, 0.4)',
              color: '#c7d2fe',
              cursor: 'pointer',
            }}
          >
            Jump to latest
          </button>
        )}
      </div>
      {errorMsg && (
        <div style={{ color: '#ef4444', fontSize: 12, marginBottom: 6 }}>
          {errorMsg}
        </div>
      )}
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        style={{
          fontSize: 13,
          maxHeight: 200,
          overflow: 'auto',
          lineHeight: 1.5,
        }}
      >
        {/* v2.1.0: utterance-grouped per-word view when v2 endpoint
            tokens_finalized is observed. Falls back to the legacy
            per-frame entry view for v1-endpoint sessions or before any
            tokens arrive. */}
        {utterances.length > 0 ? (
          <UtteranceView
            utterances={utterances}
            currentDraft={currentDraft}
          />
        ) : transcript.length === 0 ? (
          <span style={{ opacity: 0.4 }}>
            (no partials yet, speak to populate)
          </span>
        ) : (
          transcript.map((e, i) => (
            <div
              key={`${e.sequence}-${i}`}
              style={{
                opacity: e.type === 'final' ? 1 : 0.78,
                fontWeight: e.type === 'final' ? 500 : 400,
                borderLeft: e.type === 'final' ? '2px solid #4caf50' : '2px solid #1976d2',
                paddingLeft: 8,
                marginBottom: 3,
              }}
              title={`${e.type} seq=${e.sequence}${e.tokensFinalized ? ` tokens=${e.tokensFinalized.length}` : ''}`}
            >
              <SpeakerBadges speakers={e.speakers} />
              {e.text}
              {/* v2.0.0: in-flight draft tokens — model has not committed
                  these yet, may revise. Rendered in faint italic so users
                  see the leading edge of the stream without confusing it
                  with finalized text. Hidden when text_draft is empty. */}
              {e.textDraft && (
                <span style={{ opacity: 0.45, fontStyle: 'italic', marginLeft: 4 }}>
                  {e.textDraft}
                </span>
              )}
              {/* v2.0.0: EOU detection chip. Fires when the model emits
                  the <EOU> end-of-utterance token, telling the user the
                  system recognized they stopped talking. Subtle indicator
                  rather than a big banner. */}
              {e.eouDetected && (
                <span
                  style={{
                    marginLeft: 6,
                    padding: '1px 5px',
                    borderRadius: 3,
                    background: '#52525b',
                    color: '#d4d4d8',
                    fontSize: 10,
                    fontWeight: 600,
                    letterSpacing: 0.3,
                  }}
                  title="End of utterance detected by streaming model"
                >
                  EOU
                </span>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
