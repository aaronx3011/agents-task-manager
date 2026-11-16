import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import type { IncomingMessage } from "node:http";
import type { Plugin, ProxyOptions, ViteDevServer, PreviewServer } from "vite";

// The SPA is served on its own (Caddy in deployments, Vite here) and talks to
// Django on the same origin through these proxied paths (#50).
const api = process.env.ATM_API_ORIGIN || "http://localhost:8000";
// Keep the browser's Host header (changeOrigin: false): Django's CSRF and
// WebSocket Origin checks compare it with the page origin, as behind Caddy.
const backend = { target: api, changeOrigin: false };
const voiceTarget = process.env.ATM_VOICE_ORIGIN || "http://127.0.0.1:4177";
const voiceOrigin = new URL(voiceTarget).origin;
let voiceSocketProxy: Parameters<NonNullable<ProxyOptions["configure"]>>[0];
const voicePaths = new Map([
  ["/voice-agent/api/status", "GET"],
  ["/voice-agent/api/chat", "POST"],
  ["/voice-agent/api/voice", "POST"],
  ["/voice-agent/api/listen", "GET"],
]);
async function voiceAccess(req: IncomingMessage) {
  if (voicePaths.get(req.url || "") !== req.method) return 404;
  if (
    (req.method === "POST" || req.headers.upgrade) &&
    ![`http://${req.headers.host}`, `https://${req.headers.host}`].includes(
      req.headers.origin || "",
    )
  )
    return 403;
  try {
    // Reuse existing owner authorization; the voice service has shared MCP
    // credentials, so a viewer/member must not reach it through this proxy.
    const response = await fetch(api + "/api/v1/users", {
      headers: {
        cookie: req.headers.cookie || "",
        host: req.headers.host || "",
      },
      signal: AbortSignal.timeout(5000),
      redirect: "manual",
    });
    await response.body?.cancel();
    return response.ok ? 200 : response.status === 401 ? 401 : 403;
  } catch {
    return 502;
  }
}
const voice: ProxyOptions = {
  target: voiceTarget,
  changeOrigin: true,
  // We authorize upgrades before handing them to the proxy ourselves.
  ws: false,
  rewrite: (path) => path.replace(/^\/voice-agent/, ""),
  async bypass(req, res) {
    if (!res) return false;
    const status = await voiceAccess(req);
    if (status === 200) return;
    res.writeHead(status, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "Voice agent access unavailable" }));
    return req.url;
  },
  configure(proxy) {
    voiceSocketProxy = proxy;
    const headers = (request: import("node:http").ClientRequest) => {
      request.setHeader("Origin", voiceOrigin);
      request.removeHeader("Cookie");
      request.removeHeader("Authorization");
    };
    proxy.on("proxyReq", headers);
    proxy.on("proxyReqWs", headers);
  },
};
const voiceUpgrades: Plugin = {
  name: "voice-agent-upgrades",
  configureServer: installVoiceUpgrade,
  configurePreviewServer: installVoiceUpgrade,
};
function installVoiceUpgrade(server: ViteDevServer | PreviewServer) {
  server.httpServer?.on("upgrade", (req, socket, head) => {
    if (!req.url?.startsWith("/voice-agent/")) return;
    void voiceAccess(req).then((status) => {
      if (status !== 200 || req.url !== "/voice-agent/api/listen") {
        socket.end(
          `HTTP/1.1 ${status === 200 ? 404 : status} Rejected\r\nConnection: close\r\n\r\n`,
        );
        return;
      }
      req.url = "/api/listen";
      voiceSocketProxy.ws(req, socket, head);
    });
  });
}
const proxy = {
  "/voice-agent": voice,
  "/api": backend,
  "/admin": backend,
  "/static": backend,
  "/healthz": backend,
  "/ws": { ...backend, ws: true },
};

export default defineConfig({
  plugins: [react(), tailwindcss(), voiceUpgrades],
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
