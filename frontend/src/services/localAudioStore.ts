/**
 * IndexedDB-backed local-first persistence for full-audio chunks.
 *
 * Phase A.5 of the mobile roadmap: each MediaRecorder chunk produced by
 * `fullAudioRecorder.ts` lands here in addition to the server upload
 * queue. On stop we compute a final SHA-256 + byte count and POST a
 * verification payload to `/finalize-audio`; if the server confirms it
 * has everything, we wipe the local copy. If it doesn't, we either
 * backfill the missing chunks or POST the whole assembled blob via
 * `/full-audio`.
 *
 * Resume-on-reload: orphan sessions (started but never finalized + never
 * wiped) sit in IndexedDB until the user hits Resume Upload or Discard.
 * That covers the "browser crashed mid-meeting" case which today loses
 * everything between the last server-acked chunk and the crash.
 *
 * Privacy mode: when local-only, we still write here but never compute
 * the server verification. The blob is the source of truth and the
 * local STT/LLM pipeline runs against it. Caller chooses.
 *
 * Scope: desktop only. `fullAudioRecorder` callers gate on
 * `capabilityClass === 'desktop-capable'` before initialising this
 * store. Mobile browsers stay chunks-straight-to-server by design
 * (battery + storage quota + iOS tab-suspend make local-first there a
 * footgun; native iOS / Android in Phase C-1 / C-3 own that surface).
 *
 * v2 follow-up (not in this drop): OPFS for chunks > 100MB sessions
 * and a Worker-side ffmpeg.wasm assemble + re-encode pass. IndexedDB's
 * per-key blob ceiling is fine for the ~30s/chunk * 2-hour-meeting
 * envelope we care about today.
 */

export const LOCAL_AUDIO_DB_NAME = 'uc-meeting-ops-recordings';
export const LOCAL_AUDIO_DB_VERSION = 1;
export const LOCAL_AUDIO_CHUNK_STORE = 'audio-chunks';
export const LOCAL_AUDIO_META_STORE = 'session-metadata';

export interface LocalAudioChunkRecord {
  /** Server-side session id (NOT the IndexedDB key — the key is composite). */
  session_id: string;
  /** chunk_index assigned by fullAudioRecorder, 0-based, monotonic. */
  sequence_number: number;
  /** The raw MediaRecorder dataavailable Blob. WebM/Opus on Chrome/FF/Edge, MP4/AAC on Safari. */
  blob: Blob;
  /** MIME the recorder produced (`pickRecordableMime()`). Pinned per-session. */
  mime_type: string;
  /** Byte size — denormalised so getOrphanedSessions can fast-path without reading blobs. */
  bytes: number;
  /** ISO timestamp when the chunk landed (client clock). */
  recorded_at: string;
}

export interface LocalAudioSessionMeta {
  /** Server-side session id (same value the upload endpoints see). */
  session_id: string;
  /** ISO start. Used in the orphan banner ("found unfinished recording from..."). */
  started_at: string;
  /** ISO end, set when finalize() or wipe() runs. Null while still recording. */
  ended_at: string | null;
  /** Sum of chunk bytes — running total updated by appendChunk. */
  total_bytes: number;
  /** Number of chunks ingested so far. */
  chunk_count: number;
  /** MIME from the first chunk; all subsequent chunks share the recorder's MIME. */
  mime_type: string;
  /**
   * Set true once stop() succeeded AND the server confirmed it has
   * everything. Once true, the local copy is safe to wipe and the row
   * is no longer an "orphan".
   */
  finalized: boolean;
  /**
   * Set true after wipeSession() — we keep the metadata row around for
   * a short while so the UI can show "discarded" history if it wants
   * to. wiped rows are filtered out of getOrphanedSessions.
   */
  wiped: boolean;
  /** Hex SHA-256 of the concatenated chunk bytes, set at finalize. */
  client_sha256: string | null;
  /**
   * Whether this is a privacy-mode (local-only) session. Privacy
   * sessions are never wiped automatically — they're the source of
   * truth and the user wipes them manually via the Local Sessions UI.
   */
  privacy_mode: boolean;
}

export interface AssembledBlobInfo {
  blob: Blob;
  sha256: string;
  bytes: number;
  chunkCount: number;
}

let dbPromise: Promise<IDBDatabase> | null = null;

function openDb(): Promise<IDBDatabase> {
  if (typeof indexedDB === 'undefined') {
    return Promise.reject(new Error('IndexedDB unavailable in this environment.'));
  }
  if (dbPromise) return dbPromise;
  dbPromise = new Promise<IDBDatabase>((resolve, reject) => {
    const req = indexedDB.open(LOCAL_AUDIO_DB_NAME, LOCAL_AUDIO_DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(LOCAL_AUDIO_CHUNK_STORE)) {
        const chunks = db.createObjectStore(LOCAL_AUDIO_CHUNK_STORE, {
          keyPath: ['session_id', 'sequence_number'],
        });
        chunks.createIndex('session_id', 'session_id', { unique: false });
      }
      if (!db.objectStoreNames.contains(LOCAL_AUDIO_META_STORE)) {
        const meta = db.createObjectStore(LOCAL_AUDIO_META_STORE, { keyPath: 'session_id' });
        meta.createIndex('started_at', 'started_at', { unique: false });
        meta.createIndex('finalized', 'finalized', { unique: false });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => {
      dbPromise = null;
      reject(req.error ?? new Error('IndexedDB open failed.'));
    };
    req.onblocked = () => {
      dbPromise = null;
      reject(new Error('IndexedDB blocked by another tab.'));
    };
  });
  return dbPromise;
}

function awaitRequest<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error('IndexedDB request failed.'));
  });
}

async function withStores<T>(
  storeNames: string[],
  mode: IDBTransactionMode,
  run: (stores: Record<string, IDBObjectStore>, tx: IDBTransaction) => Promise<T> | T,
): Promise<T> {
  const db = await openDb();
  return new Promise<T>((resolve, reject) => {
    const tx = db.transaction(storeNames, mode);
    const stores: Record<string, IDBObjectStore> = {};
    for (const name of storeNames) stores[name] = tx.objectStore(name);
    let result: T | undefined;
    let pending = false;
    try {
      const maybe = run(stores, tx);
      if (maybe && typeof (maybe as Promise<T>).then === 'function') {
        pending = true;
        (maybe as Promise<T>).then((val) => {
          result = val;
        }).catch((err) => {
          try {
            tx.abort();
          } catch {
            /* noop */
          }
          reject(err);
        });
      } else {
        result = maybe as T;
      }
    } catch (err) {
      try {
        tx.abort();
      } catch {
        /* noop */
      }
      reject(err);
      return;
    }
    tx.oncomplete = () => resolve(result as T);
    tx.onerror = () => reject(tx.error ?? new Error('IndexedDB transaction failed.'));
    tx.onabort = () => {
      if (!pending) reject(tx.error ?? new Error('IndexedDB transaction aborted.'));
    };
  });
}

function toHex(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let out = '';
  for (let i = 0; i < bytes.length; i += 1) {
    out += bytes[i].toString(16).padStart(2, '0');
  }
  return out;
}

/**
 * SubtleCrypto.digest doesn't stream — there's no incremental API in
 * the spec. We accumulate Uint8Array views per chunk and concat at
 * finalize. For a 2-hour meeting at ~30s/chunk * ~256KB/chunk that's
 * ~60MB peak which we can hold comfortably; if we ever push past
 * that we'll need to swap to a WASM streaming SHA-256 (e.g.
 * @noble/hashes). The accumulator is a separate array so we don't
 * pin the Blob list to a synchronous loop.
 */
async function sha256OfBlobs(blobs: Blob[]): Promise<string> {
  if (!('crypto' in globalThis) || !crypto.subtle?.digest) {
    throw new Error('SubtleCrypto digest unavailable.');
  }
  // Concat all chunks into one Uint8Array. Browser memory budget is
  // generous enough for the meeting envelope we target (see comment).
  const parts: Uint8Array[] = [];
  let total = 0;
  for (const blob of blobs) {
    const buf = await blob.arrayBuffer();
    const view = new Uint8Array(buf);
    parts.push(view);
    total += view.byteLength;
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    merged.set(part, offset);
    offset += part.byteLength;
  }
  const digest = await crypto.subtle.digest('SHA-256', merged);
  return toHex(digest);
}

/** Persistence is best-effort. Callers should call `init()` once and
 *  ignore the rejection on browsers without IndexedDB. The recorder
 *  upload path is independent of local-store success. */
async function init(): Promise<void> {
  await openDb();
}

async function appendChunk(
  sessionId: string,
  seq: number,
  blob: Blob,
  mime: string,
): Promise<void> {
  const recordedAt = new Date().toISOString();
  const bytes = blob.size;
  await withStores(
    [LOCAL_AUDIO_CHUNK_STORE, LOCAL_AUDIO_META_STORE],
    'readwrite',
    async (stores) => {
      const chunkStore = stores[LOCAL_AUDIO_CHUNK_STORE];
      const metaStore = stores[LOCAL_AUDIO_META_STORE];

      const chunkRecord: LocalAudioChunkRecord = {
        session_id: sessionId,
        sequence_number: seq,
        blob,
        mime_type: mime,
        bytes,
        recorded_at: recordedAt,
      };
      chunkStore.put(chunkRecord);

      const existing = (await awaitRequest(metaStore.get(sessionId))) as
        | LocalAudioSessionMeta
        | undefined;
      const meta: LocalAudioSessionMeta = existing
        ? {
            ...existing,
            total_bytes: existing.total_bytes + bytes,
            chunk_count: existing.chunk_count + 1,
            // Preserve the original mime — first chunk wins.
            mime_type: existing.mime_type || mime,
          }
        : {
            session_id: sessionId,
            started_at: recordedAt,
            ended_at: null,
            total_bytes: bytes,
            chunk_count: 1,
            mime_type: mime,
            finalized: false,
            wiped: false,
            client_sha256: null,
            privacy_mode: false,
          };
      metaStore.put(meta);
    },
  );
}

async function createSessionMetadata(
  sessionId: string,
  startedAt: string,
  privacyMode: boolean,
): Promise<void> {
  await withStores([LOCAL_AUDIO_META_STORE], 'readwrite', async (stores) => {
    const store = stores[LOCAL_AUDIO_META_STORE];
    const existing = (await awaitRequest(store.get(sessionId))) as
      | LocalAudioSessionMeta
      | undefined;
    if (existing) {
      // If the metadata row already exists (e.g. a quick stop-restart),
      // preserve started_at + chunk counts and just refresh the privacy
      // flag if it changed.
      store.put({ ...existing, privacy_mode: privacyMode });
      return;
    }
    const meta: LocalAudioSessionMeta = {
      session_id: sessionId,
      started_at: startedAt,
      ended_at: null,
      total_bytes: 0,
      chunk_count: 0,
      mime_type: '',
      finalized: false,
      wiped: false,
      client_sha256: null,
      privacy_mode: privacyMode,
    };
    store.put(meta);
  });
}

async function getSessionMetadata(
  sessionId: string,
): Promise<LocalAudioSessionMeta | null> {
  return withStores([LOCAL_AUDIO_META_STORE], 'readonly', async (stores) => {
    const row = (await awaitRequest(stores[LOCAL_AUDIO_META_STORE].get(sessionId))) as
      | LocalAudioSessionMeta
      | undefined;
    return row ?? null;
  });
}

async function setSessionFinalized(sessionId: string, sha256: string): Promise<void> {
  await withStores([LOCAL_AUDIO_META_STORE], 'readwrite', async (stores) => {
    const store = stores[LOCAL_AUDIO_META_STORE];
    const existing = (await awaitRequest(store.get(sessionId))) as
      | LocalAudioSessionMeta
      | undefined;
    if (!existing) return;
    store.put({
      ...existing,
      finalized: true,
      ended_at: existing.ended_at || new Date().toISOString(),
      client_sha256: sha256,
    });
  });
}

async function setSessionPrivacyMode(
  sessionId: string,
  privacyMode: boolean,
): Promise<void> {
  await withStores([LOCAL_AUDIO_META_STORE], 'readwrite', async (stores) => {
    const store = stores[LOCAL_AUDIO_META_STORE];
    const existing = (await awaitRequest(store.get(sessionId))) as
      | LocalAudioSessionMeta
      | undefined;
    if (!existing) return;
    store.put({ ...existing, privacy_mode: privacyMode });
  });
}

async function setSessionWiped(sessionId: string): Promise<void> {
  await withStores([LOCAL_AUDIO_META_STORE], 'readwrite', async (stores) => {
    const store = stores[LOCAL_AUDIO_META_STORE];
    const existing = (await awaitRequest(store.get(sessionId))) as
      | LocalAudioSessionMeta
      | undefined;
    if (!existing) return;
    store.put({
      ...existing,
      wiped: true,
      ended_at: existing.ended_at || new Date().toISOString(),
    });
  });
}

async function getOrphanedSessions(): Promise<LocalAudioSessionMeta[]> {
  return withStores([LOCAL_AUDIO_META_STORE], 'readonly', async (stores) => {
    const all = (await awaitRequest(stores[LOCAL_AUDIO_META_STORE].getAll())) as
      | LocalAudioSessionMeta[]
      | undefined;
    if (!all) return [];
    // Privacy-mode sessions are NEVER orphans — they're the source of
    // truth in local-only mode and have their own "Local Sessions"
    // surface. Filter them out so the resume-on-reload banner doesn't
    // suggest uploading them to the server.
    return all
      .filter((m) => !m.finalized && !m.wiped && !m.privacy_mode && m.chunk_count > 0)
      .sort((a, b) => a.started_at.localeCompare(b.started_at));
  });
}

async function getPrivacySessions(): Promise<LocalAudioSessionMeta[]> {
  return withStores([LOCAL_AUDIO_META_STORE], 'readonly', async (stores) => {
    const all = (await awaitRequest(stores[LOCAL_AUDIO_META_STORE].getAll())) as
      | LocalAudioSessionMeta[]
      | undefined;
    if (!all) return [];
    return all
      .filter((m) => m.privacy_mode && !m.wiped)
      .sort((a, b) => b.started_at.localeCompare(a.started_at));
  });
}

async function getChunks(
  sessionId: string,
): Promise<{ seq: number; blob: Blob; mime: string }[]> {
  return withStores([LOCAL_AUDIO_CHUNK_STORE], 'readonly', async (stores) => {
    const store = stores[LOCAL_AUDIO_CHUNK_STORE];
    const index = store.index('session_id');
    const records = (await awaitRequest(index.getAll(IDBKeyRange.only(sessionId)))) as
      | LocalAudioChunkRecord[]
      | undefined;
    if (!records) return [];
    return records
      .sort((a, b) => a.sequence_number - b.sequence_number)
      .map((r) => ({ seq: r.sequence_number, blob: r.blob, mime: r.mime_type }));
  });
}

/**
 * Concatenate all stored chunks for a session into a single Blob and
 * compute its SHA-256. Used at stop() to build the verification payload
 * and on the /full-audio fallback path.
 *
 * Note: the assembled Blob is a raw concat of the MediaRecorder slices,
 * NOT a server-side ffmpeg re-mux. The server normalises to WAV before
 * the STT/diar pipeline, and the verification payload compares the
 * concat'd bytes byte-for-byte against what the server has on disk,
 * which is also the raw chunk bytes pre-reassembly. That's the same
 * surface both sides reason about.
 */
async function getAssembledBlob(sessionId: string): Promise<AssembledBlobInfo> {
  const chunks = await getChunks(sessionId);
  if (chunks.length === 0) {
    throw new Error('No chunks stored locally for this session.');
  }
  const blobs = chunks.map((c) => c.blob);
  const sha256 = await sha256OfBlobs(blobs);
  // Assembled blob — used by /full-audio fallback. Type is the first
  // chunk's MIME; all chunks in one session share a MIME from the
  // same MediaRecorder instance.
  const assembled = new Blob(blobs, { type: chunks[0].mime || 'application/octet-stream' });
  return {
    blob: assembled,
    sha256,
    bytes: assembled.size,
    chunkCount: chunks.length,
  };
}

async function wipeSession(sessionId: string): Promise<void> {
  await withStores(
    [LOCAL_AUDIO_CHUNK_STORE, LOCAL_AUDIO_META_STORE],
    'readwrite',
    async (stores) => {
      const chunkStore = stores[LOCAL_AUDIO_CHUNK_STORE];
      // IDB lets us delete via a key range on the composite key. Anything
      // with this session_id prefix is ours. We walk via the index to
      // avoid relying on key-range ordering semantics across engines.
      const index = chunkStore.index('session_id');
      const keys = (await awaitRequest(
        index.getAllKeys(IDBKeyRange.only(sessionId)),
      )) as IDBValidKey[] | undefined;
      if (keys) {
        for (const key of keys) chunkStore.delete(key);
      }
      stores[LOCAL_AUDIO_META_STORE].delete(sessionId);
    },
  );
}

async function getStorageUsage(): Promise<{ usage: number; quota: number }> {
  if (typeof navigator === 'undefined' || !navigator.storage?.estimate) {
    return { usage: 0, quota: 0 };
  }
  try {
    const est = await navigator.storage.estimate();
    return { usage: est.usage ?? 0, quota: est.quota ?? 0 };
  } catch {
    return { usage: 0, quota: 0 };
  }
}

/** Probe whether the runtime supports the local-store surface. Returns
 *  false on browsers without IndexedDB or SubtleCrypto (older Safari
 *  private browsing, jsdom in tests). Callers should refuse to enable
 *  the local-first path in that case. */
function isAvailable(): boolean {
  if (typeof indexedDB === 'undefined') return false;
  if (typeof crypto === 'undefined' || !crypto.subtle?.digest) return false;
  return true;
}

export const localAudioStore = {
  init,
  isAvailable,
  appendChunk,
  createSessionMetadata,
  getSessionMetadata,
  setSessionFinalized,
  setSessionPrivacyMode,
  setSessionWiped,
  getOrphanedSessions,
  getPrivacySessions,
  getChunks,
  getAssembledBlob,
  wipeSession,
  getStorageUsage,
};

export type LocalAudioStore = typeof localAudioStore;

// Test-only: clear the cached db promise so tests can re-open after
// indexedDB.deleteDatabase. NOT exposed in the public surface.
export function __resetForTests(): void {
  dbPromise = null;
}
