import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The SPA is served on its own (Caddy in deployments, Vite here) and talks to
// Django on the same origin through these proxied paths (#50).
const api = process.env.ATM_API_ORIGIN || "http://localhost:8000";
// Keep the browser's Host header (changeOrigin: false): Django's CSRF and
// WebSocket Origin checks compare it with the page origin, as behind Caddy.
const backend = { target: api, changeOrigin: false };
const proxy = {
  "/api": backend,
  "/admin": backend,
  "/static": backend,
  "/healthz": backend,
  "/ws": { ...backend, ws: true },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/",
  server: { proxy },
  preview: { proxy },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: "assets/app-[hash].js",
        chunkFileNames: "assets/chunks/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
