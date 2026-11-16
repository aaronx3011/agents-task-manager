import assert from "node:assert/strict";
import test from "node:test";
import { VoiceClient, voiceEvents } from "../src/voice-client.ts";

test("voice stream preserves split UTF-8, tool events and final unterminated line", async () => {
  const bytes = new TextEncoder().encode(
    '{"type":"text","delta":"¡Hola!"}\n{"type":"tool","name":"create_task"}\n{"type":"done"}',
  );
  const body = new ReadableStream({
    start(controller) {
      for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      controller.close();
    },
  });
  const events = [];
  for await (const event of voiceEvents(body)) events.push(event);
  assert.equal(events[0].delta, "¡Hola!");
  assert.deepEqual(
    events.map((event) => event.type),
    ["text", "tool", "done"],
  );
});

test("tool execution refreshes tasks and completion becomes conversation context", async (t) => {
  let refreshed = 0;
  const calls = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url, body: JSON.parse(options.body) });
    return new Response(
      '{"type":"tool","name":"task_coordinator__create_task","result":{}}\n{"type":"text","delta":"Created task 123."}\n{"type":"done"}\n',
    );
  });
  const client = new VoiceClient(
    () => {},
    () => refreshed++,
  );
  await client.send("Create a task in agents-task-manager");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/voice-agent/api/chat");
  assert.equal(refreshed, 1);
  assert.deepEqual(client.messages.at(-1), {
    role: "assistant",
    content: "Created task 123.",
  });
  assert.equal(client.active, undefined);
  client.close();
});

test("incomplete and failed chats never retry a potential task mutation", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls++;
    return new Response('{"type":"text","delta":"Creating"}\n');
  });
  const client = new VoiceClient(
    () => {},
    () => {},
  );
  await client.send("Create a task");
  assert.equal(calls, 1);
  assert.match(client.error, /Check task status/);
  assert.equal(client.messages.length, 1);
  client.close();
});

test("interrupt aborts streaming, marks partial output and releases microphone/audio on exit", async (t) => {
  let signal;
  let controller;
  t.mock.method(globalThis, "fetch", async (_, options) => {
    signal = options.signal;
    return new Response(
      new ReadableStream({
        start(stream) {
          controller = stream;
          stream.enqueue(
            new TextEncoder().encode('{"type":"text","delta":"Working"}\n'),
          );
          signal.addEventListener("abort", () =>
            stream.error(new DOMException("Aborted", "AbortError")),
          );
        },
      }),
    );
  });
  let update;
  const firstChunk = new Promise((resolve) => {
    update = resolve;
  });
  const client = new VoiceClient(
    () => {
      if (client.entries.some((entry) => entry.text === "Working")) update();
    },
    () => {},
  );
  const pending = client.send("Create a task");
  await firstChunk;
  client.stopReply();
  await pending;
  assert.equal(signal.aborted, true);
  assert.match(client.entries[1].text, /interrupted/);
  assert.equal(client.error, "");
  assert.equal(client.messages.length, 1);
  let stopped = 0;
  client.capture = {
    stream: { getTracks: () => [{ stop: () => stopped++ }] },
    ws: { close: () => stopped++ },
  };
  client.audio = { close: async () => stopped++ };
  client.close();
  assert.equal(stopped, 3);
  assert.equal(client.capture, undefined);
  assert.ok(controller);
});
