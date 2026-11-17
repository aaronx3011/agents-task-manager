import assert from "node:assert/strict";
import test from "node:test";
import { playSpeech, speechChunks } from "../src/voice-speech.ts";

const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

test("long replies, Unicode, paragraphs and unbroken URLs retain every character", () => {
  for (const text of [
    "A complete sentence. ".repeat(400),
    "paragraph\n\n".repeat(200),
    "😀".repeat(1600),
    "x".repeat(2100),
    "https://example.com/" + "a".repeat(3000),
    "中文回答。".repeat(400),
    "",
    "Short reply.",
  ]) {
    const chunks = speechChunks(text);
    assert.equal(chunks.join(""), text);
    assert.ok(chunks.every((chunk) => chunk.length > 0 && chunk.length <= 180));
    assert.ok(
      chunks.every(
        (chunk) =>
          !/[\uD800-\uDBFF]$/u.test(chunk) && !/^[\uDC00-\uDFFF]/u.test(chunk),
      ),
    );
  }
  const chunks = speechChunks("One sentence. ".repeat(100));
  assert.ok(chunks.slice(0, -1).every((chunk) => chunk.endsWith(". ")));
});

test("ordered playback prefetches only one following chunk and speaks beyond 4000 characters", async () => {
  const text = "A full sentence. ".repeat(400);
  const chunks = speechChunks(text);
  const requested = [],
    played = [],
    playing = [];
  const run = playSpeech(
    text,
    async (chunk) => {
      requested.push(chunk);
      return new Blob([chunk]);
    },
    async (blob) => {
      played.push(await blob.text());
      const completion = deferred();
      playing.push(completion);
      await completion.promise;
    },
    new AbortController().signal,
  );
  await tick();
  assert.equal(requested.length, 2);
  assert.equal(played.length, 1);
  for (let index = 0; index < chunks.length; index++) {
    assert.equal(played[index], chunks[index]);
    assert.ok(requested.length <= played.length + 1);
    playing[index].resolve();
    await tick();
  }
  await run;
  assert.equal(requested.join(""), text);
  assert.equal(played.join(""), text);
});

test("interrupt aborts in-flight prefetch and prevents stale or later playback", async () => {
  const controller = new AbortController();
  const prefetch = deferred(),
    playback = deferred();
  let requests = 0,
    played = 0,
    upstreamSignal;
  const run = playSpeech(
    "x".repeat(2800),
    async (_chunk, signal) => {
      requests++;
      upstreamSignal = signal;
      return requests === 1 ? new Blob(["first"]) : prefetch.promise;
    },
    async () => {
      played++;
      await playback.promise;
    },
    controller.signal,
  );
  await tick();
  assert.equal(requests, 2);
  controller.abort();
  assert.ok(upstreamSignal.aborted);
  prefetch.resolve(new Blob(["stale"]));
  playback.resolve();
  await assert.rejects(run, { name: "AbortError" });
  assert.equal(played, 1);
  assert.equal(requests, 2);
});

test("failed prefetched chunks stop the queue without retry or skipping", async () => {
  let requests = 0,
    played = 0;
  await assert.rejects(
    playSpeech(
      "word ".repeat(800),
      async () => {
        requests++;
        if (requests === 2) throw new Error("TTS unavailable");
        return new Blob(["first"]);
      },
      async () => {
        played++;
        await tick();
      },
      new AbortController().signal,
    ),
    /TTS unavailable/,
  );
  assert.equal(requests, 2);
  assert.equal(played, 1);
});

test("an already-cancelled queue does not request synthesis", async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    playSpeech(
      "Hello",
      async () => {
        assert.fail("unexpected synthesis");
      },
      async () => {
        assert.fail("unexpected playback");
      },
      controller.signal,
    ),
    { name: "AbortError" },
  );
});
