import { defineConfig } from "vite";
import path from "path";
import fs from "fs";

// For local HTTPS with self-signed certificate
// This allows microphone access from any local IP
export default defineConfig({
  server: {
    port: 7777,
    host: "0.0.0.0",
    https: {
      // You can generate these with:
      // openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes
      key: fs.readFileSync(path.resolve(__dirname, 'localhost-key.pem')),
      cert: fs.readFileSync(path.resolve(__dirname, 'localhost-cert.pem')),
    },
    proxy: {
      "/api": {
        target: "http://localhost:9050",
        changeOrigin: true,
        secure: false,
      },
      "/ws": {
        target: "ws://localhost:9050",
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
  }
});