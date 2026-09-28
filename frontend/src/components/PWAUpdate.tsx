import { useEffect, useState } from 'react';
import { RefreshCw, X } from 'lucide-react';
import { useRecording } from '../contexts/RecordingContext';
import { useAlwaysOn } from '../contexts/AlwaysOnContext';

/**
 * Banner that surfaces a "New version available — click to reload" prompt
 * when vite-plugin-pwa detects a fresh build on the server.
 *
 * Without this, the service worker silently caches the next deploy in the
 * background and only swaps it in when the user closes every tab and comes
 * back later. That's how Aaron got stuck on a stale bundle that still
 * referenced onnxruntime-web@1.17.0 even though the server had 1.20.1.
 *
 * Deploy-during-recording coordination
 * -------------------------------------
 * The PWA is configured `registerType: "prompt"` + workbox `skipWaiting:
 * false`, so a newly-deployed SW parks in the *waiting* state and never
 * takes over the page on its own. Activation happens only when the user
 * clicks "Reload now" here (which calls `updateSW(true)`).
 *
 * We additionally suppress the banner entirely while a recording is in
 * progress — live (`RecordingContext.isRecording`) OR always-on capture
 * (`AlwaysOnContext.state` in starting/recording/paused/stopping). Applying
 * a new SW mid-capture would swap cached chunks out from under the active
 * recording. The `needRefresh` flag stays latched; the moment recording
 * stops the banner appears ("an update was ready during your recording —
 * apply now?"). When no recording is active it behaves as before: prompt
 * as soon as the update is detected.
 *
 * The registerSW helper is provided by virtual:pwa-register at build time;
 * we import it dynamically + tolerate its absence in case PWA gets disabled.
 */
const ACTIVE_ALWAYS_ON_STATES = ['starting', 'recording', 'paused', 'stopping'];

export function PWAUpdate() {
  const [needRefresh, setNeedRefresh] = useState(false);
  const [updateFn, setUpdateFn] = useState<(() => Promise<void>) | null>(null);
  const [heldDuringRecording, setHeldDuringRecording] = useState(false);

  const { isRecording } = useRecording();
  const { state: alwaysOnState } = useAlwaysOn();
  const recordingInProgress =
    isRecording || ACTIVE_ALWAYS_ON_STATES.includes(alwaysOnState);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const mod = await import(/* @vite-ignore */ 'virtual:pwa-register');
        const fn = mod.registerSW({
          immediate: true,
          onNeedRefresh: () => { if (!cancelled) setNeedRefresh(true); },
        });
        if (!cancelled) {
          setUpdateFn(() => async () => {
            await fn(true);
            window.location.reload();
          });
        }
      } catch {
        // PWA disabled at build time — banner just never fires.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // Remember that an update was held back because the user was recording, so
  // we can tailor the copy when the banner finally surfaces.
  useEffect(() => {
    if (needRefresh && recordingInProgress) setHeldDuringRecording(true);
  }, [needRefresh, recordingInProgress]);

  // Latch the update, but don't show the banner mid-recording. The waiting
  // SW stays parked (skipWaiting:false), so nothing activates until the user
  // chooses to reload — which is only offered once recording has stopped.
  if (!needRefresh || !updateFn || recordingInProgress) return null;

  return (
    <div className="fixed bottom-4 right-4 z-[60] max-w-sm rounded-xl border border-purple-500/40 bg-zinc-900/95 px-4 py-3 shadow-xl backdrop-blur-sm">
      <div className="flex items-start gap-3">
        <RefreshCw className="mt-0.5 h-5 w-5 shrink-0 text-purple-400" />
        <div className="flex-1 text-sm text-zinc-100">
          <div className="font-medium">
            {heldDuringRecording ? 'Update ready' : 'New version available'}
          </div>
          <div className="mt-0.5 text-xs text-zinc-400">
            {heldDuringRecording
              ? 'We held an update while you were recording. Reload now to apply it.'
              : 'We just shipped an update. Reload to get the latest fixes.'}
          </div>
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              onClick={updateFn}
              className="rounded-md bg-purple-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-purple-500"
            >
              Reload now
            </button>
            <button
              type="button"
              onClick={() => setNeedRefresh(false)}
              className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            >
              Later
            </button>
          </div>
        </div>
        <button
          type="button"
          onClick={() => setNeedRefresh(false)}
          className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300"
          aria-label="Dismiss"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
}
