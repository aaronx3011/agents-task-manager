// Voice Lab's 24 kHz PCM16 microphone protocol.
class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frame = new Int16Array(960);
    this.index = 0;
  }
  process(inputs) {
    const channel = inputs[0][0];
    if (!channel) return true;
    for (const value of channel) {
      const clipped = Math.max(-1, Math.min(1, value));
      this.frame[this.index++] =
        clipped < 0 ? clipped * 32768 : clipped * 32767;
      if (this.index === this.frame.length) {
        const buffer = this.frame.buffer;
        this.port.postMessage(buffer, [buffer]);
        this.frame = new Int16Array(960);
        this.index = 0;
      }
    }
    return true;
  }
}
registerProcessor("voice-capture", VoiceCapture);
