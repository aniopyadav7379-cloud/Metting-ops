// SELF-DESTRUCT SERVICE WORKER  (served at /sw.js)
//
// 2026-06-04: the v3.22.2 incident shipped a kill-switch as `sw-kill.js`
// but 404'd `/sw.js`. Zombies registered during the PWA-enabled window
// (v3.20.x -> v3.22.1) poll THEIR script URL, which is `/sw.js` — so a
// 404 there never unregisters them; they keep serving a stale precached
// app shell (record-first + mobile-capable never reach those devices).
//
// Serving this self-destruct worker AT /sw.js fixes that: a zombie's next
// update check fetches this script, the browser installs it (skipWaiting),
// and on activate it (a) unregisters itself, (b) clears EVERY cache, and
// (c) reloads every open window. After one trip the device has no
// controlling worker and no stale precache, and the next load fetches the
// current app from origin. There is NO fetch handler, so this worker can
// never intercept a request or re-enter the v3.22.2 fetch loop. The app
// itself does not register any SW (VitePWA is disabled), so nothing gets
// re-registered after the reload — this runs exactly once per device.

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // 1. Unregister so the browser stops routing fetches to this SW
      //    after activation completes.
      try {
        await self.registration.unregister();
      } catch (e) {
        // ignore — best effort
      }

      // 2. Delete every cache the previous (workbox) SW populated.
      try {
        const cacheNames = await caches.keys();
        await Promise.all(cacheNames.map((name) => caches.delete(name)));
      } catch (e) {
        // ignore — best effort
      }

      // 3. Reload every open client once so they pick up unmediated
      //    network responses with no SW in the middle.
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

// Intentionally NO fetch handler.
