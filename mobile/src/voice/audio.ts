// Pure audio helpers for the PM voice agent. The server accepts only mono 24 kHz
// PCM16 WAV (coordinator/voice_runtime.py), like the web's voice-audio.ts.

export const WAV_RATE = 24000;
export const MAX_CLIP_SECONDS = 15;

/** One microphone buffer from expo-audio's AudioStream (int16 encoding). */
export interface PcmBuffer {
  data: ArrayBuffer;
  sampleRate: number;
  channels: number;
}

/**
 * Mix captured int16 buffers to mono and resample them to 24 kHz. The device
 * may not honour the requested rate, so each buffer's reported rate is used.
 */
export function monoSamples(buffers: PcmBuffer[], rate = WAV_RATE): Float32Array {
  const parts: Float32Array[] = [];
  let run: number[] = [];
  let runRate = 0;
  const flush = () => {
    if (run.length) parts.push(resample(Float32Array.from(run), runRate, rate));
    run = [];
  };
  for (const buffer of buffers) {
    const channels = Math.max(1, buffer.channels);
    if (buffer.sampleRate !== runRate) {
      flush();
      runRate = buffer.sampleRate;
    }
    const view = new DataView(buffer.data);
    const frames = Math.floor(buffer.data.byteLength / 2 / channels);
    for (let frame = 0; frame < frames; frame++) {
      let sum = 0;
      for (let channel = 0; channel < channels; channel++)
        sum += view.getInt16((frame * channels + channel) * 2, true);
      run.push(sum / channels / 32768);
    }
  }
  flush();
  const total = parts.reduce((length, part) => length + part.length, 0);
  const mono = new Float32Array(total);
  let offset = 0;
  for (const part of parts) {
    mono.set(part, offset);
    offset += part.length;
  }
  return mono;
}

/** Linear-interpolation resampling; speech-to-text needs no better. */
export function resample(samples: Float32Array, from: number, to: number): Float32Array {
  if (!(from > 0) || from === to || !samples.length) return samples;
  const length = Math.max(1, Math.round((samples.length * to) / from));
  const output = new Float32Array(length);
  const step = from / to;
  for (let index = 0; index < length; index++) {
    const position = index * step;
    const left = Math.min(Math.floor(position), samples.length - 1);
    const right = Math.min(left + 1, samples.length - 1);
    const weight = position - left;
    output[index] = samples[left] * (1 - weight) + samples[right] * weight;
  }
  return output;
}

/** A mono PCM16 WAV file, as bytes. */
export function wavBytes(samples: Float32Array, rate = WAV_RATE): Uint8Array {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const label = (offset: number, value: string) => {
    for (let index = 0; index < value.length; index++)
      view.setUint8(offset + index, value.charCodeAt(index));
  };
  label(0, "RIFF");
  view.setUint32(4, buffer.byteLength - 8, true);
  label(8, "WAVEfmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  label(36, "data");
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, index) => {
    const value = Number.isFinite(sample) ? Math.max(-1, Math.min(1, sample)) : 0;
    view.setInt16(44 + index * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
  });
  return new Uint8Array(buffer);
}

/** Preserve the full reply while keeping each synthesis request short (web voice-speech.ts). */
export function speechChunks(text: string, limit = 900): string[] {
  if (!Number.isInteger(limit) || limit < 2 || limit > 2000)
    throw new Error("Invalid speech chunk limit");
  const chunks: string[] = [];
  while (text.length > limit) {
    const prefix = text.slice(0, limit);
    let end = 0;
    for (const match of prefix.matchAll(/[.!?。！？]["'”’)]*\s+|\n\s*\n/gu))
      end = match.index + match[0].length;
    if (end < limit / 3) {
      end = prefix.lastIndexOf(" ") + 1;
      if (end < limit / 3) end = limit;
    }
    // Never divide a surrogate pair in an unbroken URL, word or emoji sequence.
    if (/[\uD800-\uDBFF]/u.test(text[end - 1])) end--;
    chunks.push(text.slice(0, end));
    text = text.slice(end);
  }
  if (text) chunks.push(text);
  return chunks;
}

/** React Native's AbortSignal may lack throwIfAborted(). */
export function stopIfAborted(signal: AbortSignal) {
  if (signal.aborted) throw new Error("Stopped");
}

/** Synthesize and play chunks in order; at most one is prefetched while another plays. */
export async function playSpeech<T>(
  text: string,
  synthesize: (text: string, signal: AbortSignal) => Promise<T>,
  play: (audio: T, signal: AbortSignal) => Promise<void>,
  signal: AbortSignal,
) {
  const chunks = speechChunks(text).filter((chunk) => chunk.trim());
  const load = (index: number) =>
    synthesize(chunks[index], signal).then(
      (audio) => ({ audio, error: undefined }),
      (error: unknown) => ({ audio: undefined, error }),
    );
  if (!chunks.length) return;
  stopIfAborted(signal);
  let next = load(0);
  for (let index = 0; index < chunks.length; index++) {
    const result = await next;
    stopIfAborted(signal);
    if (result.audio === undefined) throw result.error;
    if (index + 1 < chunks.length) next = load(index + 1);
    await play(result.audio, signal);
    stopIfAborted(signal);
  }
}
