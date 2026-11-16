// Browser adapter for the existing Voice Lab protocol. No provider or MCP
// credentials live here; /voice-agent is the web front door's service proxy.
export const VOICE_API = "/voice-agent/api";
export type VoiceEntry = { role: "user" | "assistant" | "tool"; text: string };
export type VoiceStatus = {
  policy?: { version: string; sha256: string };
  speech?: boolean;
  ollama?: boolean;
  modelReady?: boolean;
  model?: string;
  mcp?: Record<string, { connected: boolean }>;
};
type Reply = {
  controller: AbortController;
  text: string;
  pending: string;
  queue: string[];
  sources: Set<AudioBufferSourceNode>;
  nextTime: number;
  entry: number;
  speaking?: boolean;
  finished?: boolean;
};
type Capture = {
  stream?: MediaStream;
  input?: MediaStreamAudioSourceNode;
  worklet?: AudioWorkletNode;
  ws?: WebSocket;
  ready?: boolean;
};

export async function* voiceEvents(body: ReadableStream<Uint8Array>) {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      pending += decoder.decode(value, { stream: !done });
      let boundary;
      while ((boundary = pending.indexOf("\n")) !== -1) {
        const line = pending.slice(0, boundary);
        pending = pending.slice(boundary + 1);
        if (line.trim()) yield JSON.parse(line);
      }
      if (done) break;
    }
    if (pending.trim()) yield JSON.parse(pending);
  } finally {
    reader.releaseLock();
  }
}

export class VoiceClient {
  entries: VoiceEntry[] = [];
  messages: { role: "user" | "assistant"; content: string }[] = [];
  active?: Reply;
  capture?: Capture;
  audio?: AudioContext;
  hearing = false;
  transcribing = false;
  muted = false;
  closed = false;
  error = "";
  private changed: () => void;
  private toolUsed: () => void;
  constructor(changed: () => void, toolUsed: () => void) {
    this.changed = changed;
    this.toolUsed = toolUsed;
  }

  get state() {
    return this.hearing
      ? "Listening to you"
      : this.transcribing
        ? "Transcribing…"
        : this.active?.sources.size
          ? "Speaking"
          : this.active
            ? "Thinking…"
            : this.capture?.ready
              ? "Listening"
              : this.capture
                ? "Connecting microphone…"
                : "Ready to talk";
  }
  update() {
    if (!this.closed) this.changed();
  }
  fail(error: unknown) {
    this.error =
      error instanceof Error ? error.message : "Voice agent unavailable";
    this.update();
  }
  async unlockAudio() {
    this.audio ||= new AudioContext({ sampleRate: 24000 });
    await this.audio.resume();
    if (this.audio.sampleRate !== 24000)
      throw new Error(
        "This browser could not open 24 kHz audio. Try Chrome or Safari.",
      );
  }
  stopReply() {
    const reply = this.active;
    this.active = undefined;
    reply?.controller.abort();
    for (const source of reply?.sources || []) {
      try {
        source.stop();
      } catch {
        /* already ended */
      }
    }
    if (reply?.text)
      this.entries[reply.entry].text =
        reply.text + (reply.finished ? "" : " … [interrupted]");
    this.update();
  }
  setMuted(muted: boolean) {
    this.muted = muted;
    if (muted && this.active) {
      this.active.queue.length = 0;
      this.active.pending = "";
      for (const source of this.active.sources) source.stop();
    }
    this.update();
  }
  async send(text: string) {
    text = text.trim();
    if (this.closed || !text || text.length > 4000) return;
    this.stopReply();
    this.error = "";
    this.messages.push({ role: "user", content: text });
    this.entries.push({ role: "user", text });
    const reply: Reply = {
      controller: new AbortController(),
      text: "",
      pending: "",
      queue: [],
      sources: new Set(),
      nextTime: 0,
      entry: this.entries.length,
    };
    this.entries.push({ role: "assistant", text: "" });
    this.active = reply;
    this.update();
    try {
      const response = await fetch(VOICE_API + "/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          history: this.messages.slice(-12).map((message) => ({
            ...message,
            content: message.content.slice(0, 4000),
          })),
        }),
        signal: reply.controller.signal,
      });
      if (!response.ok || !response.body)
        throw new Error(
          "Voice chat unavailable. Check the Voice Lab connection.",
        );
      let done = false;
      for await (const event of voiceEvents(response.body)) {
        if (this.active !== reply) return;
        if (event.type === "error") throw new Error(event.error);
        if (event.type === "done") done = true;
        if (event.type === "tool") {
          this.entries.push({
            role: "tool",
            text:
              event.name +
              (event.result?.error || event.result?.isError
                ? " · reported an error"
                : " · result received"),
          });
          this.toolUsed();
        }
        if (event.type === "text") {
          reply.text += event.delta;
          this.entries[reply.entry].text = reply.text;
          reply.pending += event.delta;
          this.queueSpeech(reply);
        }
        this.update();
      }
      if (!done || !reply.text.trim())
        throw new Error(
          "The agent did not finish its reply. Check task status before repeating a task creation request.",
        );
      this.messages.push({ role: "assistant", content: reply.text });
      this.queueSpeech(reply, true);
    } catch (error) {
      if (!reply.controller.signal.aborted) this.fail(error);
    } finally {
      reply.finished = true;
      this.finish(reply);
    }
  }
  finish(reply: Reply) {
    if (
      this.active === reply &&
      reply.finished &&
      !reply.speaking &&
      !reply.sources.size
    )
      this.active = undefined;
    this.update();
  }
  queueSpeech(reply: Reply, flush = false) {
    if (this.muted || !this.audio || this.audio.state !== "running") {
      reply.pending = "";
      return;
    }
    while (reply.pending) {
      const boundary = reply.pending.match(/[.!?。！？](?=\s|$)|\n/);
      let end = boundary ? boundary.index! + 1 : 0;
      if (!end && reply.pending.length >= 150)
        end = reply.pending.lastIndexOf(" ", 145) + 1 || 145;
      if (!end && flush) end = reply.pending.length;
      if (!end) break;
      const text = reply.pending.slice(0, end).trim();
      reply.pending = reply.pending.slice(end);
      if (text) reply.queue.push(text);
    }
    void this.speak(reply);
  }
  async speak(reply: Reply) {
    if (
      reply.speaking ||
      reply.controller.signal.aborted ||
      !reply.queue.length
    )
      return;
    reply.speaking = true;
    try {
      while (reply.queue.length) {
        const response = await fetch(VOICE_API + "/voice", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: reply.queue.shift() }),
          signal: reply.controller.signal,
        });
        if (!response.ok || !response.body)
          throw new Error(
            "Spoken reply unavailable; the transcript is still visible.",
          );
        const reader = response.body.getReader();
        let pending = new Uint8Array();
        try {
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            const data = new Uint8Array(pending.length + value.length);
            data.set(pending);
            data.set(value, pending.length);
            const length = data.length - (data.length % 2);
            if (length && !this.muted && !reply.controller.signal.aborted)
              this.play(reply, data.subarray(0, length));
            pending = data.slice(length);
          }
        } finally {
          reader.releaseLock();
        }
      }
    } catch (error) {
      if (!reply.controller.signal.aborted) this.fail(error);
      reply.queue.length = 0;
    } finally {
      reply.speaking = false;
      this.finish(reply);
    }
  }
  play(reply: Reply, pcm: Uint8Array) {
    const audio = this.audio!;
    const buffer = audio.createBuffer(1, pcm.length / 2, 24000);
    const view = new DataView(pcm.buffer, pcm.byteOffset, pcm.byteLength);
    const floats = buffer.getChannelData(0);
    for (let i = 0; i < floats.length; i++)
      floats[i] = view.getInt16(i * 2, true) / 32768;
    const source = audio.createBufferSource();
    source.buffer = buffer;
    source.connect(audio.destination);
    reply.nextTime = Math.max(reply.nextTime, audio.currentTime + 0.035);
    source.start(reply.nextTime);
    reply.nextTime += buffer.duration;
    reply.sources.add(source);
    source.onended = () => {
      reply.sources.delete(source);
      source.disconnect();
      this.finish(reply);
    };
    this.update();
  }
  stopMic() {
    const capture = this.capture;
    this.capture = undefined;
    capture?.stream?.getTracks().forEach((track) => track.stop());
    capture?.input?.disconnect();
    capture?.worklet?.disconnect();
    capture?.ws?.close();
    this.hearing = false;
    this.transcribing = false;
    this.update();
  }
  async startMic() {
    if (this.capture || this.closed) return;
    this.error = "";
    const capture: Capture = {};
    this.capture = capture;
    this.update();
    try {
      if (!navigator.mediaDevices?.getUserMedia)
        throw new Error(
          "Microphone access needs HTTPS or localhost and a supported browser.",
        );
      await this.unlockAudio();
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      if (this.capture !== capture) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      capture.stream = stream;
      await this.audio!.audioWorklet.addModule(
        new URL("./voice-mic.js", import.meta.url),
      );
      if (this.capture !== capture) return;
      const ws = new WebSocket(
        `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${VOICE_API}/listen`,
      );
      capture.ws = ws;
      ws.onopen = () =>
        ws.send(
          JSON.stringify({
            type: "session.update",
            session: {
              modalities: ["text"],
              turn_detection: {
                type: "server_vad",
                threshold: 0.5,
                prefix_padding_ms: 300,
                silence_duration_ms: 420,
                max_turn_duration_ms: 25000,
              },
            },
          }),
        );
      ws.onmessage = ({ data }) => {
        if (this.capture !== capture) return;
        try {
          const event = JSON.parse(data);
          if (event.type === "session.updated" && !capture.ready) {
            capture.input = this.audio!.createMediaStreamSource(stream);
            capture.worklet = new AudioWorkletNode(
              this.audio!,
              "voice-capture",
              { channelCount: 1, outputChannelCount: [1] },
            );
            capture.worklet.port.onmessage = ({ data: frame }) => {
              if (ws.readyState === WebSocket.OPEN && ws.bufferedAmount < 64000)
                ws.send(
                  JSON.stringify({
                    type: "input_audio_buffer.append",
                    audio: btoa(String.fromCharCode(...new Uint8Array(frame))),
                  }),
                );
            };
            capture.input.connect(capture.worklet);
            capture.worklet.connect(this.audio!.destination);
            capture.ready = true;
          }
          if (event.type === "input_audio_buffer.speech_started") {
            this.hearing = true;
            this.transcribing = false;
            this.stopReply();
          }
          if (event.type === "input_audio_buffer.speech_stopped") {
            this.hearing = false;
            this.transcribing = true;
          }
          if (
            event.type ===
            "conversation.item.input_audio_transcription.completed"
          ) {
            this.transcribing = false;
            if (event.transcript?.trim()) void this.send(event.transcript);
          }
          if (event.type === "error") {
            this.fail(
              new Error(event.error?.message || "Speech recognition failed"),
            );
            this.stopMic();
          }
          this.update();
        } catch (error) {
          this.fail(error);
          this.stopMic();
        }
      };
      ws.onerror = () => {
        if (this.capture === capture) {
          this.fail(
            new Error("Microphone connection failed. Check Voice Lab."),
          );
          this.stopMic();
        }
      };
      ws.onclose = () => {
        if (this.capture === capture) {
          this.fail(
            new Error("Microphone disconnected. Start it again to reconnect."),
          );
          this.stopMic();
        }
      };
    } catch (error) {
      if (this.capture === capture) {
        this.fail(error);
        this.stopMic();
      }
    }
  }
  close() {
    this.closed = true;
    this.stopReply();
    this.stopMic();
    void this.audio?.close();
  }
}
