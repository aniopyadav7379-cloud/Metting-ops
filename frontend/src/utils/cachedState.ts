/**
 * Hydrated state — paint last-known-good data immediately on mount.
 *
 * Eliminates the "blank → spinner → content" flash on data-fetching
 * pages. The pattern is:
 *
 *   const [rooms, setRooms, hydrated] = useHydratedState<Room[]>(
 *     cacheKey('rooms', orgSlug),
 *     [],
 *   );
 *
 *   useEffect(() => {
 *     fetchRooms().then(setRooms);
 *   }, [orgSlug]);
 *
 * Behavior:
 *  - On first paint, returns whatever was cached for this exact key
 *    (or the default if nothing cached / stale / corrupt).
 *  - `setRooms(...)` also writes to localStorage so the next mount
 *    is instant.
 *  - 24-hour staleness window. Older caches are discarded.
 *  - Auth events (login/logout) broadcast `meetingops:auth-cache-clear`
 *    which wipes every hydrated key. This keeps user-A's data from
 *    flashing onto user-B's screen.
 *  - Org switch is handled by the key itself — callers MUST include
 *    `orgSlug` in the key via `cacheKey()`. When the user switches
 *    orgs the key changes, so the cache for the wrong org is never
 *    even read.
 *  - SSR-safe (returns default + a no-op setter when window is missing).
 *
 * NOT a general-purpose cache. Don't put transcripts, summaries, audio
 * blobs, or any PHI here — only list/index data that's fine to leak
 * to localStorage on the user's own device.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

const CACHE_PREFIX = 'meetingops.hydrated.';
const CACHE_VERSION = 1;
const STALE_AFTER_MS = 24 * 60 * 60 * 1000; // 24h

interface CacheEnvelope<T> {
  v: number;
  t: number; // timestamp ms
  d: T;
}

/**
 * Build a cache key. The orgSlug (or any other partition like userId)
 * MUST be included so wrong-org data is never read on mount.
 *
 * Returns null when orgSlug is null — callers should treat that as
 * "skip the hydration, just use the default".
 */
export function cacheKey(
  namespace: string,
  orgSlug: string | null | undefined,
  ...rest: Array<string | number | null | undefined>
): string | null {
  if (!orgSlug) return null;
  const tail = rest
    .filter((p) => p !== undefined && p !== null && p !== '')
    .map((p) => String(p))
    .join(':');
  return tail ? `${namespace}:${orgSlug}:${tail}` : `${namespace}:${orgSlug}`;
}

function readCache<T>(key: string | null): T | undefined {
  if (!key || typeof window === 'undefined') return undefined;
  try {
    const raw = window.localStorage.getItem(CACHE_PREFIX + key);
    if (!raw) return undefined;
    const parsed = JSON.parse(raw) as CacheEnvelope<T>;
    if (!parsed || typeof parsed !== 'object') return undefined;
    if (parsed.v !== CACHE_VERSION) return undefined;
    if (typeof parsed.t !== 'number') return undefined;
    if (Date.now() - parsed.t > STALE_AFTER_MS) {
      // Tidy as we go so stale entries don't pile up.
      try { window.localStorage.removeItem(CACHE_PREFIX + key); } catch { /* noop */ }
      return undefined;
    }
    return parsed.d;
  } catch {
    return undefined;
  }
}

function writeCache<T>(key: string | null, value: T): void {
  if (!key || typeof window === 'undefined') return;
  try {
    const envelope: CacheEnvelope<T> = { v: CACHE_VERSION, t: Date.now(), d: value };
    window.localStorage.setItem(CACHE_PREFIX + key, JSON.stringify(envelope));
  } catch {
    // Quota / private-mode. Silently drop — we just lose the speedup,
    // the page still works.
  }
}

/**
 * Wipe every hydrated cache entry. Called on login/logout so user A's
 * data never flashes into user B's view.
 *
 * Tolerant of stubbed localStorage implementations that omit `length`
 * or `key()` — used in unit tests.
 */
export function clearAllHydratedCaches(): void {
  if (typeof window === 'undefined') return;
  try {
    const store = window.localStorage;
    if (!store || typeof store.length !== 'number' || typeof store.key !== 'function') {
      return;
    }
    const toRemove: string[] = [];
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i);
      if (k && k.startsWith(CACHE_PREFIX)) toRemove.push(k);
    }
    for (const k of toRemove) {
      try { store.removeItem(k); } catch { /* noop */ }
    }
  } catch {
    /* noop */
  }
}

export const HYDRATED_CACHE_CLEAR_EVENT = 'meetingops:auth-cache-clear';

/**
 * Like `useState<T>` but seeds from localStorage if a fresh cached
 * value exists for `key`. Every state update is mirrored back to
 * localStorage. The third tuple element is `true` when the initial
 * paint used cached data (handy for skipping skeletons on first render).
 */
export function useHydratedState<T>(
  key: string | null,
  defaultValue: T,
): [T, (next: T | ((prev: T) => T)) => void, boolean] {
  // Capture the key used at mount so we don't fight with React StrictMode
  // double-invokes and so a key change cleanly re-mounts state.
  const initialKeyRef = useRef(key);
  const initial = readCache<T>(key);
  const [value, setValue] = useState<T>(() => (initial !== undefined ? initial : defaultValue));
  const [hydratedFromCache] = useState<boolean>(initial !== undefined);

  // If the key changes after mount (e.g. org switch), reset state to
  // whatever the new key has cached, or default.
  useEffect(() => {
    if (key === initialKeyRef.current) return;
    initialKeyRef.current = key;
    const fresh = readCache<T>(key);
    setValue(fresh !== undefined ? fresh : defaultValue);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  // Wipe state if auth cache-clear event fires (login/logout).
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const onClear = () => setValue(defaultValue);
    window.addEventListener(HYDRATED_CACHE_CLEAR_EVENT, onClear);
    return () => window.removeEventListener(HYDRATED_CACHE_CLEAR_EVENT, onClear);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setAndPersist = useCallback(
    (next: T | ((prev: T) => T)) => {
      setValue((prev) => {
        const resolved =
          typeof next === 'function' ? (next as (p: T) => T)(prev) : next;
        writeCache(key, resolved);
        return resolved;
      });
    },
    [key],
  );

  return [value, setAndPersist, hydratedFromCache];
}

/**
 * Imperative cache write — for code paths that need to update the cache
 * without going through React state (e.g. background polls that don't
 * want to re-render). Rare; prefer the hook.
 */
export function writeHydratedCache<T>(key: string | null, value: T): void {
  writeCache(key, value);
}
