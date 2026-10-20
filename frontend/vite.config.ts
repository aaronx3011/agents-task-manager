import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ command }) => ({
  plugins: [react(), tailwindcss()],
  base: command === "serve" ? "/" : "/static/coordinator/app/",
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/admin": "http://localhost:8000",
      "/dashboard/workspace": "http://localhost:8000",
      "/static/admin": "http://localhost:8000",
      "/static/rest_framework": "http://localhost:8000",
      "/ws": { target: "http://localhost:8000", ws: true },
    },
  },
  build: {
    outDir: "../coordinator/static/coordinator/app",
    emptyOutDir: true,
    rollupOptions: {
      input: "src/main.tsx",
      output: {
        entryFileNames: "app.js",
        chunkFileNames: "chunks/[name]-[hash].js",
        assetFileNames: (asset) =>
          asset.names.some((name) => name.endsWith(".css"))
            ? "app.css"
            : "assets/[name]-[hash][extname]",
      },
    },
  },
}));
