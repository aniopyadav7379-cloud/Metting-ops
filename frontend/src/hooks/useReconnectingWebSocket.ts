/**
 * useReconnectingWebSocket
 * ========================
 *
 * Phase B.5 production-polish reconnect hook for the server-live streaming
 * WebSocket (`/ws/sessions/{id}/live`) and any other WS consumers in the
 * app that need durable connections.
 *
 * Behavior
 * --------
 *   - On `onclose` with a *normal* code (1000 client-end, 1001 going_away)
 *     we do NOT reconnect. The server signalled a clean close.
 *   - On `onclose` with a *custom application* code in the RFC 6455 4xxx
 *     range (4001 unauth, 4003 tier_insufficient, 4429 rate_limited,
 *     etc.) we do NOT reconnect either. These are intentional rejections;
 *     reconnecting would just spam the server with denied handshakes.
 *     The caller can read the close code off `onClose` and surface the
 *     error to the user.
 *   - On any other close code (1006 abnormal, 1011 server error, network
 *     drop) we DO reconnect with exponential backoff: 500 ms, 1 s, 2 s,
 *     4 s, ... capped at `maxBackoffMs` (default 30 s), up to `maxRetries`
 *     (default 5) attempts. After that we fire `onGiveUp` and stop.
 *
 * The hook exposes a stable `send` / `close` API and a `retries` counter
 * the UI can read to surface "Reconnecting in 4s..." text. Each retry
 * fires `onReconnect(attempt, delayMs)` for the same purpose.
 *
 * Lifecycle
 * ---------
 * `url` is captured into a ref so the hook keeps reconnecting to whatever
 * URL was passed on the *first* mount. If the consumer wants to change
 * URLs (e.g. switch sessions), they should unmount + remount this hook
 * — usually that means changing a `key` on the parent component, or
 * teardown via `close()` and reopen with new opts. We intentionally do
 * NOT auto-re-establish on URL changes; that's a footgun for live
 * audio streams where the URL changes per session.
 *
 * Caller-supplied callbacks (`onOpen`, `onMessage`, `onClose`, etc.) are
 * read off a ref so the latest values are always called without the
 * effect re-running every time the parent re-renders.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

export interface ReconnectingWebSocketOptions {
  /** WS URL (`ws://` or `wss://`). Captured on first mount. */
  url: string;
  /** Optional subprotocols passed to `new WebSocket()`. */
  protocols?: string | string[];
  /** Max reconnect attempts before giving up. Default 5. */
  maxRetries?: number;
  /** First backoff delay in ms. Default 500. */
  initialBackoffMs?: number;
  /** Cap on the exponential backoff in ms. Default 30000. */
  maxBackoffMs?: number;
  /** binaryType for received frames. Default 'arraybuffer'. */
  binaryType?: BinaryType;
  /** Optional: skip reconnect entirely for these extra close codes. */
  noReconnectCodes?: number[];
  onOpen?: (ev: Event) => void;
  onClose?: (ev: CloseEvent) => void;
  onMessage?: (ev: MessageEvent) => void;
  onError?: (ev: Event) => void;
  /** Called immediately before each reconnect attempt fires. */
  onReconnect?: (attempt: number, delayMs: number) => void;
  /** Called after maxRetries have been exhausted. */
  onGiveUp?: () => void;
}

export interface UseReconnectingWebSocketReturn {
  /** Send a payload; returns true if the socket was OPEN when called. */
  send: (data: string | ArrayBufferLike | Blob | ArrayBufferView) => boolean;
  /** Close the socket and stop reconnecting. */
  close: (code?: number, reason?: string) => void;
  /** Current WS readyState (0..3). 3 (CLOSED) if no socket yet. */
  readyState: number;
  /** How many reconnect attempts have fired since the last open. */
  retries: number;
  /** True after maxRetries have been exhausted. */
  gaveUp: boolean;
}

/** Close codes that should NEVER trigger a reconnect. */
const CLEAN_CLOSE_CODES = new Set<number>([1000, 1001]);

/**
 * Decide whether a given close should be retried. Custom 4xxx codes
 * (RFC 6455 application range) are *intentional* rejections from our
 * own server — never reconnect on those. 1000 + 1001 are clean. Everything
 * else (1006 abnormal, 1011 server error, network drops) gets retried.
 */
function shouldReconnect(code: number, extraNoReconnect?: number[]): boolean {
  if (CLEAN_CLOSE_CODES.has(code)) return false;
  if (code >= 4000 && code <= 4999) return false;
  if (extraNoReconnect && extraNoReconnect.includes(code)) return false;
  return true;
}

/**
 * Compute the backoff delay for the given attempt number (0-indexed).
 * 0 -> initialBackoffMs, 1 -> 2x, 2 -> 4x, ..., capped at maxBackoffMs.
 */
function computeBackoff(attempt: number, initialMs: number, maxMs: number): number {
  const raw = initialMs * Math.pow(2, attempt);
  return Math.min(raw, maxMs);
}

export function useReconnectingWebSocket(
  opts: ReconnectingWebSocketOptions
): UseReconnectingWebSocketReturn {
  const {
    url,
    protocols,
    maxRetries = 5,
    initialBackoffMs = 500,
    maxBackoffMs = 30000,
    binaryType = 'arraybuffer',
    noReconnectCodes,
  } = opts;

  // Stash the URL once. The hook does NOT chase URL changes; consumers
  // who want a new URL should unmount + remount via `key`.
  const urlRef = useRef<string>(url);
  const protocolsRef = useRef<string | string[] | undefined>(protocols);
  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef<number>(0);
  const closedByUserRef = useRef<boolean>(false);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Latest-callback ref: the consumer can pass new callbacks on every
  // render without triggering reconnects.
  const cbRef = useRef({
    onOpen: opts.onOpen,
    onClose: opts.onClose,
    onMessage: opts.onMessage,
    onError: opts.onError,
    onReconnect: opts.onReconnect,
    onGiveUp: opts.onGiveUp,
  });
  cbRef.current = {
    onOpen: opts.onOpen,
    onClose: opts.onClose,
    onMessage: opts.onMessage,
    onError: opts.onError,
    onReconnect: opts.onReconnect,
    onGiveUp: opts.onGiveUp,
  };

  const [readyState, setReadyState] = useState<number>(
    typeof WebSocket !== 'undefined' ? WebSocket.CLOSED : 3
  );
  const [retries, setRetries] = useState<number>(0);
  const [gaveUp, setGaveUp] = useState<boolean>(false);

  const cleanupTimer = () => {
    if (reconnectTimerRef.current !== null) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
  };

  const openSocket = useCallback(() => {
    cleanupTimer();
    if (closedByUserRef.current) return;

    let ws: WebSocket;
    try {
      ws = protocolsRef.current
        ? new WebSocket(urlRef.current, protocolsRef.current)
        : new WebSocket(urlRef.current);
    } catch (err) {
      // `new WebSocket()` itself can throw on a malformed URL. Treat as
      // an abnormal close + retry on backoff like we would for 1006.
      console.warn('[useReconnectingWebSocket] construct failed', err);
      scheduleReconnect();
      return;
    }
    ws.binaryType = binaryType;
    wsRef.current = ws;
    setReadyState(ws.readyState);

    ws.onopen = (ev) => {
      // Reset retry counter on a successful open. The next disconnect
      // gets a fresh ladder starting at initialBackoffMs.
      retriesRef.current = 0;
      setRetries(0);
      setGaveUp(false);
      setReadyState(ws.readyState);
      cbRef.current.onOpen?.(ev);
    };

    ws.onmessage = (ev) => {
      cbRef.current.onMessage?.(ev);
    };

    ws.onerror = (ev) => {
      cbRef.current.onError?.(ev);
    };

    ws.onclose = (ev) => {
      setReadyState(ws.readyState);
      cbRef.current.onClose?.(ev);

      if (closedByUserRef.current) {
        // User called close() explicitly; don't reconnect.
        return;
      }

      if (!shouldReconnect(ev.code, noReconnectCodes)) {
        // Clean close or intentional 4xxx rejection. Stop.
        return;
      }

      scheduleReconnect();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [binaryType]);

  const scheduleReconnect = useCallback(() => {
    if (closedByUserRef.current) return;
    if (retriesRef.current >= maxRetries) {
      setGaveUp(true);
      cbRef.current.onGiveUp?.();
      return;
    }
    const attempt = retriesRef.current;
    const delay = computeBackoff(attempt, initialBackoffMs, maxBackoffMs);
    retriesRef.current = attempt + 1;
    setRetries(retriesRef.current);
    cbRef.current.onReconnect?.(retriesRef.current, delay);

    reconnectTimerRef.current = setTimeout(() => {
      openSocket();
    }, delay);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [maxRetries, initialBackoffMs, maxBackoffMs, openSocket]);

  // Open on mount, tear down on unmount.
  useEffect(() => {
    closedByUserRef.current = false;
    openSocket();
    return () => {
      closedByUserRef.current = true;
      cleanupTimer();
      const ws = wsRef.current;
      if (ws && ws.readyState <= WebSocket.OPEN) {
        try {
          ws.close(1000, 'component unmount');
        } catch {
          // swallow
        }
      }
      wsRef.current = null;
    };
    // We intentionally don't depend on opts.* here — the URL is captured
    // on first mount per the lifecycle contract above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const send = useCallback(
    (data: string | ArrayBufferLike | Blob | ArrayBufferView): boolean => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return false;
      ws.send(data);
      return true;
    },
    []
  );

  const close = useCallback((code?: number, reason?: string) => {
    closedByUserRef.current = true;
    cleanupTimer();
    const ws = wsRef.current;
    if (ws && ws.readyState <= WebSocket.OPEN) {
      try {
        ws.close(code ?? 1000, reason ?? 'client close');
      } catch {
        // swallow
      }
    }
  }, []);

  return { send, close, readyState, retries, gaveUp };
}

// Exported for tests + advanced callers that want to compute the next
// backoff outside the hook (e.g. for a countdown UI).
export { computeBackoff, shouldReconnect };
