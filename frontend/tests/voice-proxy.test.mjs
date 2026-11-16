import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "node:http";
import { createHash } from "node:crypto";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function listen(t, server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => {
    server.closeAllConnections();
    server.close();
  });
  return `http://127.0.0.1:${server.address().port}`;
}
async function start(t, command, args, env, ready) {
  const child = spawn(command, args, {
    cwd: root,
    env: { ...process.env, ...env },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let output = "";
  child.stdout.on("data", (data) => (output += data));
  child.stderr.on("data", (data) => (output += data));
  t.after(async () => {
    if (child.exitCode === null) {
      const exited = once(child, "exit");
      child.kill("SIGTERM");
      await exited;
    }
  });
  for (let tries = 0; tries < 100; tries++) {
    if (child.exitCode !== null) throw new Error(output);
    try {
      await fetch(ready);
      return;
    } catch {
      await delay(100);
    }
  }
  throw new Error(output || "Proxy did not start");
}

for (const kind of ["vite", "caddy"]) {
  test(
    `${kind} voice proxy enforces access/origin and relays streaming HTTP and microphone upgrades`,
    { skip: kind === "caddy" && !process.env.CADDY_BIN },
    async (t) => {
      const observed = [];
      const authServer = createServer((req, res) => {
        assert.equal(req.url, "/api/v1/users");
        assert.equal(req.headers.upgrade, undefined);
        res.writeHead(
          req.headers.cookie === "sessionid=owner"
            ? 200
            : req.headers.cookie
              ? 403
              : 401,
          { "Content-Type": "application/json" },
        );
        res.end("[]");
      });
      // ASGI interprets Upgrade on the authorization subrequest as a WebSocket
      // to /api/v1/users and rejects it. Model that real behavior here.
      authServer.on("upgrade", (_, socket) =>
        socket.end(
          "HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\n",
        ),
      );
      const api = await listen(t, authServer);
      const agent = createServer((req, res) => {
        observed.push({ path: req.url, headers: req.headers });
        res.writeHead(200, {
          "Content-Type":
            req.url === "/api/chat"
              ? "application/x-ndjson"
              : "application/json",
        });
        res.end(
          req.url === "/api/chat"
            ? '{"type":"text","delta":"Task 296 is in progress."}\n{"type":"done"}\n'
            : '{"speech":true}',
        );
      });
      agent.on("upgrade", (req, socket) => {
        observed.push({ path: req.url, headers: req.headers });
        const accept = createHash("sha1")
          .update(
            req.headers["sec-websocket-key"] +
              "258EAFA5-E914-47DA-95CA-C5AB0DC85B11",
          )
          .digest("base64");
        socket.write(
          `HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: ${accept}\r\n\r\n`,
        );
        socket.on("data", () => socket.end());
        t.after(() => socket.destroy());
      });
      const voice = await listen(t, agent);
      const reservation = createServer();
      reservation.listen(0, "127.0.0.1");
      await once(reservation, "listening");
      const port = reservation.address().port;
      await new Promise((resolve) => reservation.close(resolve));
      const origin = `http://127.0.0.1:${port}`;
      if (kind === "vite") {
        await start(
          t,
          process.execPath,
          [
            "frontend/node_modules/vite/bin/vite.js",
            "preview",
            "--config",
            "frontend/vite.config.ts",
            "--outDir",
            "frontend/dist",
            "--host",
            "127.0.0.1",
            "--port",
            String(port),
            "--strictPort",
          ],
          { ATM_API_ORIGIN: api, ATM_VOICE_ORIGIN: voice },
          origin + "/voice-agent/api/status",
        );
      } else {
        const directory = await mkdtemp(resolve(tmpdir(), "voice-proxy-"));
        t.after(() => rm(directory, { recursive: true, force: true }));
        const config = resolve(directory, "Caddyfile");
        await writeFile(
          config,
          `{\n admin off\n auto_https off\n}\nimport ${root}/deploy/web/routes.caddy\n:${port} {\n import app\n}\n`,
        );
        await start(
          t,
          process.env.CADDY_BIN,
          ["run", "--config", config, "--adapter", "caddyfile"],
          {
            API_UPSTREAM: api.slice(7),
            VOICE_UPSTREAM: voice,
            VOICE_HOST: new URL(voice).host,
            VOICE_ORIGIN: voice,
          },
          origin + "/voice-agent/api/status",
        );
      }
      const endpoint = origin + "/voice-agent/api/status";
      assert.equal((await fetch(endpoint)).status, 401);
      assert.equal(
        (await fetch(endpoint, { headers: { Cookie: "sessionid=viewer" } }))
          .status,
        403,
      );
      const headers = {
        Cookie: "sessionid=owner",
        Origin: origin,
        "Content-Type": "application/json",
      };
      assert.equal((await fetch(endpoint, { headers })).status, 200);
      assert.equal(
        (
          await fetch(origin + "/voice-agent/api/chat", {
            method: "POST",
            headers: { ...headers, Origin: "https://foreign.example" },
            body: "{}",
          })
        ).status,
        403,
      );
      assert.equal(
        (
          await fetch(origin + "/voice-agent/api/chat", {
            method: "POST",
            headers: { Cookie: headers.Cookie },
            body: "{}",
          })
        ).status,
        403,
      );
      const response = await fetch(origin + "/voice-agent/api/chat", {
        method: "POST",
        headers,
        body: "{}",
      });
      assert.equal(response.status, 200);
      assert.match(await response.text(), /Task 296 is in progress/);
      assert.equal(
        (await fetch(origin + "/voice-agent/unknown", { headers })).status,
        404,
      );
      // Native HTTP upgrade exercises proxy header rewriting without a speech runtime.
      const { request } = await import("node:http");
      await new Promise((resolve, reject) => {
        const req = request(origin + "/voice-agent/api/listen", {
          headers: {
            ...headers,
            Upgrade: "websocket",
            Connection: "Upgrade",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            "Sec-WebSocket-Version": "13",
          },
        });
        req.on("upgrade", (response, socket) => {
          assert.equal(response.statusCode, 101);
          socket.destroy();
          resolve();
        });
        req.on("response", (response) =>
          reject(new Error(`Upgrade rejected: ${response.statusCode}`)),
        );
        req.on("error", reject);
        req.end();
      });
      for (const observation of observed) {
        assert.equal(observation.headers.host, new URL(voice).host);
        assert.equal(observation.headers.origin, voice);
        assert.equal(observation.headers.cookie, undefined);
        assert.equal(observation.headers.authorization, undefined);
      }
      assert.ok(observed.some((item) => item.path === "/api/listen"));
    },
  );
}
