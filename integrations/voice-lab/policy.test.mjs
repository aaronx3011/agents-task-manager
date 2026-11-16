import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { configureServer, install } from "./install.mjs";
import { system, policyHash } from "./pm-policy.mjs";
import { createHash } from "node:crypto";

test("policy installation is repeatable, preserves routes and rejects unknown backend source", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "voice-policy-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const original = `const system = 'You are a friendly voice assistant. Old policy';
const messages = [{role:'system',content:system}];
const status = {modelReady:tags?.models?.some(item => item.name === model) ?? false, mcp:mcp.status()};
const route = '/api/chat';
`;
  await writeFile(join(directory, "server.mjs"), original);
  await install(directory);
  const first = await readFile(join(directory, "server.mjs"), "utf8");
  await install(directory);
  assert.equal(await readFile(join(directory, "server.mjs"), "utf8"), first);
  assert.ok(first.includes("const route = '/api/chat';"));
  assert.ok(first.includes("policy:{version:policyVersion,sha256:policyHash}"));
  assert.equal(
    await readFile(join(directory, "pm-policy.mjs"), "utf8"),
    await readFile(new URL("./pm-policy.mjs", import.meta.url), "utf8"),
  );
  assert.throws(
    () => configureServer("const system = 'Somebody else changed this';"),
    /Unrecognized/,
  );
  assert.equal(policyHash, createHash("sha256").update(system).digest("hex"));
});
