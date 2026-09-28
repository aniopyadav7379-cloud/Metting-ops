// v3.22.2 KILL-SWITCH SERVICE WORKER
//
// 2026-05-30 incident: existing service worker on bigboy + VPS got stuck
// in a tight registration loop (browsers hammering GET /sw.js > 10x/sec).
// Root cause traced to workbox-window's auto-update path interacting with
// clientsClaim:true + skipWaiting:false in a way that pinned the client
// in a "controller never stabilizes" state for some users (especially
// after a chain of fast deploys v3.20.x -> v3.22.1).
//
// This file replaces the workbox-generated sw.js. On install it skipWaits
// immediately. On activate it (a) unregisters itself, (b) clears EVERY
// cache the page may have populated (workbox precache + runtime), and
// (c) reloads every client window. After one trip through this SW the
// user's browser has no controlling worker and no stale precache, and
// the next page load fetches fresh assets directly from the origin.
//
// Once everyone's cycled through (~24 h with the prior max-age=14400
// cache window, or sooner if they refresh), the next deploy can put a
// real PWA SW back. Until then, the site behaves as a non-PWA SPA.

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // 1. Unregister this service worker so the browser stops handing
      //    fetches to it after this activation completes.
      try {
        await self.registration.unregister();
      } catch (e) {
        // ignore — best effort
      }

      // 2. Delete every cache the previous SW (or this one) created.
      try {
        const cacheNames = await caches.keys();
        await Promise.all(cacheNames.map((name) => caches.delete(name)));
      } catch (e) {
        // ignore — best effort
      }

      // 3. Force-reload every open client so they pick up unmediated
      //    network responses without the SW in the middle. Use a single
      //    navigate per client; if navigate fails (e.g. cross-origin),
      //    fall back to postMessage so the page can decide.
      try {
        const clients = await self.clients.matchAll({
          includeUncontrolled: true,
          type: "window",
        });
        await Promise.all(
          clients.map(async (client) => {
            try {
              await client.navigate(client.url);
            } catch (e) {
              try {
                client.postMessage({ type: "SW_KILLSWITCH_RELOAD" });
              } catch (_) {}
            }
          })
        );
      } catch (e) {
        // ignore — best effort
      }
    })()
  );
});

// Intentionally NO fetch handler. The browser falls back to direct
// network for every request, which is what we want during recovery.
