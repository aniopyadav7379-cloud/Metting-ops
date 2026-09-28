import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import './utils/installFetchInterceptor'
import App from './App.tsx'
import { track } from './utils/posthog'

// Stale-bundle recovery: when a deploy replaces the JS chunks, an already-open
// tab can fail to lazy-load a now-deleted chunk ("Failed to fetch dynamically
// imported module") and white-screen. Reload ONCE to pick up the fresh bundle.
// A short time-window guard prevents a reload loop if a chunk is genuinely
// missing for another reason.
function reloadForStaleBundle(reason: string) {
  try {
    const KEY = 'mo:preload-reload-ts';
    const last = Number(sessionStorage.getItem(KEY) || 0);
    if (Date.now() - last > 10000) {
      sessionStorage.setItem(KEY, String(Date.now()));
      // eslint-disable-next-line no-console
      console.warn('[meeting-ops] reloading to recover from a stale bundle:', reason);
      window.location.reload();
    }
  } catch {
    /* sessionStorage unavailable — best-effort, do nothing */
  }
}
window.addEventListener('vite:preloadError', (event) => {
  event.preventDefault();
  reloadForStaleBundle('vite:preloadError');
});
window.addEventListener('unhandledrejection', (event) => {
  const msg = String((event && (event.reason?.message || event.reason)) || '');
  if (/Failed to fetch dynamically imported module|error loading dynamically imported module|Importing a module script failed/i.test(msg)) {
    reloadForStaleBundle('dynamic-import-failure');
    return;
  }
  // eslint-disable-next-line no-console
  console.error('[meeting-ops] unhandled promise rejection', event.reason);
  track('frontend_unhandled_rejection', { message: msg.slice(0, 500) });
});
window.addEventListener('error', (event) => {
  // eslint-disable-next-line no-console
  console.error('[meeting-ops] uncaught window error', event.error || event.message);
  track('frontend_uncaught_error', { message: String(event.message || '').slice(0, 500) });
});

const root = document.getElementById('root');
if (root) {
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
