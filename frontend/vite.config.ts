import { defineConfig } from "vite";
import path from "path";
import { VitePWA } from "vite-plugin-pwa";

const backendUrl = process.env.VITE_BACKEND_URL || "http://localhost:9050";

export default defineConfig({
  plugins: [
    VitePWA({
      // v3.22.2 hotfix 2026-05-30: PWA temporarily disabled. The workbox
      // service worker on bigboy + VPS got stuck in a tight register/
      // claim/re-register loop (10+ /sw.js polls per second, page never
      // settled). Suspected interaction between workbox-window's update
      // path + clientsClaim:true after a chain of fast deploys
      // (v3.20.x -> v3.22.1). Production was unusable; we shipped a
      // kill-switch sw.js + nginx 404 on /sw.js to break the loop.
      //
      // Permanent re-introduction needs root-cause: reproduce the loop
      // in dev, narrow workbox version vs clientsClaim vs registerType,
      // ship in a focused hotfix release. Until then, PWA build is OFF
      // (injectRegister:false produces a build with no SW reference);
      // the SPA still works fine, just no precache / no offline.
      //
      // Original `registerType: "prompt"` + PWAUpdate banner logic
      // preserved in src/components/PWAUpdate.tsx — dynamic import of
      // virtual:pwa-register falls into its catch block when the plugin
      // is disabled, so the component is a no-op without further change.
      registerType: "prompt",
      injectRegister: false,
      disable: true,
      manifest: false,
      includeAssets: [
        "manifest.webmanifest",
        "icons/icon-192.png",
        "icons/icon-512.png",
        "icons/icon-512-maskable.png",
        "icons/apple-touch-icon.png",
        "vite.svg",
      ],
      workbox: {
        globPatterns: ["**/*.{js,css,html,svg,png,webmanifest,woff,woff2}"],
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/ws\//, /^\/oauth2\//, /^\/realms\//],
        cleanupOutdatedCaches: true,
        clientsClaim: true,
        // skipWaiting stays false so the new SW waits until PWAUpdate
        // explicitly tells it to activate (never mid-recording). clientsClaim
        // still applies on that deferred activation so the reload picks up
        // the new SW immediately.
        skipWaiting: false,
        maximumFileSizeToCacheInBytes: 6 * 1024 * 1024,
        runtimeCaching: [
          {
            urlPattern: ({ url, request }) =>
              request.method === "GET" &&
              (url.pathname === "/api/auth/me" || url.pathname === "/api/system/pipeline"),
            handler: "NetworkFirst",
            options: {
              cacheName: "meet-ops-api-getters",
              networkTimeoutSeconds: 4,
              cacheableResponse: { statuses: [0, 200] },
              expiration: { maxEntries: 32, maxAgeSeconds: 60 * 5 },
            },
          },
          {
            urlPattern: ({ url, request }) =>
              request.method === "GET" &&
              url.pathname.startsWith("/api/") &&
              !url.pathname.startsWith("/api/uploads"),
            handler: "NetworkFirst",
            options: {
              cacheName: "meet-ops-api-read",
              networkTimeoutSeconds: 4,
              cacheableResponse: { statuses: [0, 200] },
              expiration: { maxEntries: 64, maxAgeSeconds: 60 * 60 },
            },
          },
          {
            urlPattern: ({ request }) => request.destination === "image",
            handler: "CacheFirst",
            options: {
              cacheName: "meet-ops-images",
              cacheableResponse: { statuses: [0, 200] },
              expiration: { maxEntries: 64, maxAgeSeconds: 60 * 60 * 24 * 30 },
            },
          },
        ],
      },
      devOptions: {
        enabled: false,
      },
    }),
  ],
  server: {
    port: 7777,
    host: "0.0.0.0",
    proxy: {
      "/api": {
        target: backendUrl,
        changeOrigin: true,
        secure: false,
      },
      "/ws": {
        target: backendUrl.replace('http', 'ws'),
        ws: true,
        changeOrigin: true,
        secure: false,
      }
    }
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src")
    }
  },
  build: {
    rollupOptions: {
      output: {
        // Keep feature-only runtimes out of the bootstrap graph and give the
        // long-lived vendor code stable cache boundaries. Route components are
        // lazy in AppRouterSimplified; these names prevent Rollup from folding
        // their dependencies back into an unrelated page chunk.
        manualChunks(id) {
          if (!id.includes('node_modules')) return;
          if (id.includes('@huggingface/transformers')) return 'transformers-runtime';
          if (id.includes('onnxruntime-web')) return 'onnx-runtime';
          if (id.includes('@mlc-ai/web-llm')) return 'web-llm-runtime';
          if (/(react-force-graph|3d-force-graph|three|d3-|force-graph)/.test(id)) {
            return 'knowledge-graph-runtime';
          }
          if (id.includes('jspdf')) return 'report-runtime';
          if (id.includes('react-markdown') || id.includes('remark-')) return 'markdown-runtime';
          if (id.includes('recharts')) return 'charts-runtime';
          if (id.includes('lucide-react')) return 'icons-runtime';
          if (id.includes('@radix-ui')) return 'radix-runtime';
          if (id.includes('react-router') || /node_modules\/react(-dom)?\//.test(id)) {
            return 'react-runtime';
          }
        },
      },
    },
  },
});
