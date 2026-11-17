import { api, unwrap } from "./api-client";
import type { Schemas } from "./api-client";
import { playSpeech } from "./voice-speech";
import { microphoneWav } from "./voice-audio";

export type PMStatus = Schemas["VoiceStatusOutput"];
type Entry = { role: "user" | "assistant" | "tool"; text: string };
type Grant = { id: string; tool_token: string };

export class PMConversation {
  entries: Entry[] = [];
  status: "disconnected" | "connecting" | "connected" = "disconnected";
  mode = "listening";
  textOnly = true;
  muted = false;
  micMuted = false;
  error = "";
  pending = false;
  private grant?: Grant;
  private generation = 0;
  private closed = false;
  private controller = new AbortController();
  private stream?: MediaStream;
  private context?: AudioContext;
  private recorder?: MediaRecorder;
  private timer?: ReturnType<typeof setInterval>;
  private speechController?: AbortController;
  private starting?: Promise<void>;

  constructor(
    private changed: () => void,
    private toolUsed: () => void,
  ) {}
  update() {
    if (!this.closed) this.changed();
  }
  get state() {
    if (this.status === "connecting") return "Connecting to PM…";
    if (this.status !== "connected") return "Ready to talk";
    if (this.pending)
      return this.mode === "transcribing" ? "Transcribing" : "Thinking";
    if (this.textOnly) return "Text conversation connected";
    if (this.mode === "speaking") return "Speaking";
    if (this.mode === "synthesizing") return "Preparing speech";
    return this.micMuted ? "Microphone muted" : "Listening";
  }
  private options() {
    return {
      headers: { "X-ATM-Voice-Token": this.grant?.tool_token ?? "" },
      signal: this.controller.signal,
    };
  }
  private current(generation: number) {
    return !this.closed && generation === this.generation;
  }
  private fail(error: unknown) {
    this.error = error instanceof Error ? error.message : "PM request failed";
    // A tool write may have committed before a later completion/speech failure.
    this.toolUsed();
    this.update();
  }
  async start(textOnly: boolean) {
    if (this.closed) return;
    if (this.starting) return this.starting;
    if (this.grant && this.textOnly === textOnly) return;
    const generation = this.generation;
    this.starting = (async () => {
      this.status = "connecting";
      this.error = "";
      this.update();
      try {
        // Switching from text to voice preserves the server-owned history.
        if (!this.grant) {
          await unwrap(api.GET("/api/v1/auth/csrf"));
          const grant = await unwrap(
            api.POST("/api/v1/voice/sessions", {
              body: { text_only: textOnly },
              signal: this.controller.signal,
            }),
          );
          if (!this.current(generation)) {
            await this.revoke(grant);
            return;
          }
          this.grant = grant;
          this.entries = [];
        }
        this.textOnly = textOnly;
        if (!textOnly) await this.microphone(generation);
        if (this.current(generation)) this.status = "connected";
      } catch (error) {
        if (this.current(generation)) {
          await this.stop();
          this.fail(error);
        }
      } finally {
        this.starting = undefined;
        this.update();
      }
    })();
    return this.starting;
  }
  private async microphone(generation: number) {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    if (!this.current(generation)) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    this.stream = stream;
    const context = new AudioContext();
    this.context = context;
    await context.resume();
    if (!this.current(generation)) return;
    const analyser = context.createAnalyser();
    context.createMediaStreamSource(stream).connect(analyser);
    analyser.fftSize = 2048;
    const samples = new Float32Array(analyser.fftSize);
    const mime = ["audio/webm", "audio/mp4", "audio/ogg"].find((type) =>
      MediaRecorder.isTypeSupported(type),
    );
    if (!mime)
      throw new Error(
        "This browser cannot record a supported audio format. Use typed chat.",
      );
    let heard = false,
      lastVoice = 0,
      began = 0;
    const record = () => {
      if (this.pending || this.micMuted || !this.current(generation)) return;
      const recorder = new MediaRecorder(stream, { mimeType: mime });
      this.recorder = recorder;
      const chunks: Blob[] = [];
      heard = false;
      began = performance.now();
      recorder.ondataavailable = (event) => {
        if (event.data.size) chunks.push(event.data);
      };
      recorder.onstop = () => {
        if (
          this.current(generation) &&
          heard &&
          !this.micMuted &&
          !this.pending
        )
          void this.transcribe(new Blob(chunks, { type: mime }), generation);
      };
      recorder.start();
    };
    record();
    this.timer = setInterval(() => {
      if (!this.current(generation) || this.pending || this.micMuted) return;
      if (this.recorder?.state !== "recording") {
        record();
        return;
      }
      analyser.getFloatTimeDomainData(samples);
      const rms = Math.sqrt(
        samples.reduce((sum, value) => sum + value * value, 0) / samples.length,
      );
      const now = performance.now();
      if (rms > 0.025) {
        heard = true;
        lastVoice = now;
        this.interrupt();
      }
      if ((heard && now - lastVoice > 900) || now - began > 15000)
        this.recorder?.stop();
    }, 100);
  }
  finishVoiceTurn() {
    if (this.recorder?.state === "recording") this.recorder.stop();
  }
  private async transcribe(blob: Blob, generation: number) {
    this.pending = true;
    this.mode = "transcribing";
    this.update();
    try {
      const wav = await microphoneWav(blob);
      if (!this.current(generation)) return;
      const form = new FormData();
      form.append("file", wav, "recording.wav");
      const result = await unwrap(
        api.POST("/api/v1/voice/transcriptions", {
          ...this.options(),
          body: { file: "recording" },
          bodySerializer: () => form,
        }),
      );
      if (this.current(generation) && result.text.trim())
        await this.turn(result.text, generation);
    } catch (error) {
      if (this.current(generation)) this.fail(error);
    } finally {
      if (this.current(generation)) {
        this.pending = false;
        if (this.mode === "transcribing" || this.mode === "thinking")
          this.mode = "listening";
        this.update();
      }
    }
  }
  async send(text: string) {
    if (this.closed || this.pending || !text.trim()) return;
    if (!this.grant) await this.start(true);
    if (!this.grant || this.closed || this.pending) return;
    const generation = this.generation;
    this.pending = true;
    this.finishVoiceTurn();
    try {
      await this.turn(text.trim(), generation);
    } catch (error) {
      if (this.current(generation)) this.fail(error);
    } finally {
      if (this.current(generation)) {
        this.pending = false;
        this.update();
      }
    }
  }
  private async turn(text: string, generation: number) {
    this.interrupt();
    this.error = "";
    this.mode = "thinking";
    this.entries.push({ role: "user", text });
    this.update();
    const reply = await unwrap(
      api.POST("/api/v1/voice/chat", { ...this.options(), body: { text } }),
    );
    if (!this.current(generation)) return;
    reply.events.forEach((event) =>
      this.entries.push({
        role: "tool",
        text: `${event.tool} · ${event.is_error ? "reported an error" : "result received"}`,
      }),
    );
    this.entries.push({ role: "assistant", text: reply.text });
    this.toolUsed();
    this.update();
    if (!this.textOnly && !this.muted) void this.speak(reply.text, generation);
  }
  private async speak(text: string, generation: number) {
    this.interrupt();
    const controller = new AbortController();
    this.speechController = controller;
    this.mode = "synthesizing";
    this.update();
    try {
      await playSpeech(
        text,
        (chunk, signal) =>
          unwrap(
            api.POST("/api/v1/voice/speech", {
              ...this.options(),
              signal,
              body: { text: chunk },
              parseAs: "blob",
            }),
          ),
        (blob, signal) => this.playBlob(blob, signal),
        controller.signal,
      );
    } catch (error) {
      if (!controller.signal.aborted && this.current(generation)) {
        controller.abort();
        this.fail(
          new Error(
            `Spoken reply stopped: ${error instanceof Error ? error.message : "audio failed"}. The full reply remains in the transcript.`,
          ),
        );
      }
    } finally {
      if (this.speechController === controller) {
        this.speechController = undefined;
        this.mode = "listening";
        this.update();
      }
    }
  }
  private playBlob(blob: Blob, signal: AbortSignal): Promise<void> {
    return new Promise((resolve, reject) => {
      signal.throwIfAborted();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      let settled = false;
      const finish = (error?: unknown) => {
        if (settled) return;
        settled = true;
        audio.onended = null;
        audio.onerror = null;
        signal.removeEventListener("abort", abort);
        audio.pause();
        URL.revokeObjectURL(url);
        if (error) reject(error);
        else resolve();
      };
      const abort = () => finish(signal.reason);
      audio.onended = () => finish();
      audio.onerror = () =>
        finish(new Error("Could not play local speech audio"));
      signal.addEventListener("abort", abort, { once: true });
      this.mode = "speaking";
      this.update();
      void audio.play().catch(finish);
    });
  }
  private interrupt() {
    this.speechController?.abort();
    this.speechController = undefined;
    if (this.mode === "speaking" || this.mode === "synthesizing") {
      this.mode = "listening";
      this.update();
    }
  }
  setMuted(muted: boolean) {
    this.muted = muted;
    if (muted) this.interrupt();
    this.update();
  }
  toggleMic() {
    this.micMuted = !this.micMuted;
    this.stream?.getTracks().forEach((track) => {
      track.enabled = !this.micMuted;
    });
    if (this.micMuted) this.finishVoiceTurn();
    this.update();
  }
  private async revoke(grant?: Grant) {
    if (grant)
      await api
        .DELETE("/api/v1/voice/sessions/{id}", {
          params: { path: { id: grant.id } },
        })
        .catch(() => {});
  }
  async stop() {
    ++this.generation;
    this.controller.abort();
    this.controller = new AbortController();
    clearInterval(this.timer);
    this.finishVoiceTurn();
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = undefined;
    void this.context?.close();
    this.context = undefined;
    this.interrupt();
    const grant = this.grant;
    this.grant = undefined;
    this.status = "disconnected";
    this.pending = false;
    this.micMuted = false;
    this.update();
    await this.revoke(grant);
  }
  async clear() {
    await this.stop();
    this.entries = [];
    this.error = "";
    this.update();
  }
  close() {
    this.closed = true;
    void this.stop();
  }
}
