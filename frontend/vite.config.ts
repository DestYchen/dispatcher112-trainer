import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";

const backend = process.env.BACKEND_URL ?? "http://backend:8000";
const https =
  process.env.TLS_ENABLED === "1"
    ? {
        key: readFileSync("/run/tls/tls.key"),
        cert: readFileSync("/run/tls/tls.crt"),
      }
    : undefined;

export default defineConfig({
  plugins: [react()],
  cacheDir: process.env.VITE_CACHE_DIR ?? "node_modules/.vite",
  server: {
    https,
    watch: { usePolling: true, interval: 300 },
    port: 5173,
    strictPort: true,
    proxy: {
      "/healthz": backend,
      "/api": { target: backend, ws: true },
      "/media": backend,
    },
  },
  test: {
    maxWorkers: 2,
    environment: "jsdom",
    setupFiles: "./src/test-setup.ts",
    restoreMocks: true,
    clearMocks: true,
  },
});
