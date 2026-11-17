import assert from "node:assert/strict";
import test from "node:test";
import { pcmWav } from "../src/voice-audio.ts";

test("microphone WAV is mono 24kHz PCM16LE with correct lengths and sample clipping", async () => {
  const samples = new Float32Array([-2, -1, -0.5, 0, 0.5, 1, 2, NaN]);
  const blob = pcmWav(samples);
  assert.equal(blob.type, "audio/wav");
  const bytes = await blob.arrayBuffer();
  const view = new DataView(bytes);
  const text = (start, length) =>
    new TextDecoder().decode(bytes.slice(start, start + length));
  assert.equal(text(0, 4), "RIFF");
  assert.equal(text(8, 8), "WAVEfmt ");
  assert.equal(text(36, 4), "data");
  assert.equal(view.getUint32(4, true), bytes.byteLength - 8);
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 24000);
  assert.equal(view.getUint32(28, true), 48000);
  assert.equal(view.getUint16(32, true), 2);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), samples.length * 2);
  assert.deepEqual(
    Array.from(samples, (_, index) => view.getInt16(44 + index * 2, true)),
    [-32768, -32768, -16384, 0, 16384, 32767, 32767, 0],
  );
});
