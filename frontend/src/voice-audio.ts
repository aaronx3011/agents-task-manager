/** Voice Lab's recognition input is mono PCM16 at 24 kHz, transported as WAV here. */
export function pcmWav(samples: Float32Array): Blob {
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
  view.setUint32(24, 24000, true);
  view.setUint32(28, 48000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  label(36, "data");
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((sample, index) => {
    const value = Number.isFinite(sample)
      ? Math.max(-1, Math.min(1, sample))
      : 0;
    view.setInt16(
      44 + index * 2,
      Math.round(value * (value < 0 ? 32768 : 32767)),
      true,
    );
  });
  return new Blob([buffer], { type: "audio/wav" });
}

export async function microphoneWav(clip: Blob): Promise<Blob> {
  // decodeAudioData resamples to this context's rate. Mixing channels explicitly
  // keeps Safari MP4 and Chromium WebM capture on the same server-side contract.
  const decoder = new OfflineAudioContext(1, 1, 24000);
  const audio = await decoder.decodeAudioData(await clip.arrayBuffer());
  if (audio.sampleRate !== 24000 || !audio.length || audio.duration > 30)
    throw new Error("Microphone audio must be at most 30 seconds at 24 kHz");
  const mono = new Float32Array(audio.length);
  for (let channel = 0; channel < audio.numberOfChannels; channel++) {
    const input = audio.getChannelData(channel);
    for (let index = 0; index < mono.length; index++)
      mono[index] += input[index] / audio.numberOfChannels;
  }
  return pcmWav(mono);
}
