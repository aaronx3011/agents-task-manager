import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const api = process.env.ATM_API_ORIGIN || "http://localhost:8000";
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
