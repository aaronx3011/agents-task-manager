import { api, unwrap } from "./api-client";
import type { Schemas } from "./api-client";
import { playSpeech } from "./voice-speech";
import { microphoneWav } from "./voice-audio";

export type PMStatus = Schemas["VoiceStatusOutput"];
type Entry = { role: "user" | "assistant" | "tool"; text: string };
type Grant = { id: string; tool_token: string };
type Capture = {
  recorder: MediaRecorder;
  chunks: Blob[];
  generation: number;
  release?: "send" | "cancel";
  finished: boolean;
  stopped: boolean;
};

export class PMConversation {
  entries: Entry[] = [];
  status: "disconnected" | "connecting" | "connected" = "disconnected";
  mode = "listening";
  textOnly = true;
  muted = false;
  micMuted = false;
  capturing = false;
  captureReady = false;
  captureLimited = false;
  error = "";
  notice = "";
  pending = false;
  private grant?: Grant;
  private generation = 0;
  private closed = false;
  private controller = new AbortController();
  private stream?: MediaStream;
  private context?: AudioContext;
  private capture?: Capture;
  private samples?: Float32Array<ArrayBuffer>;
  private mime?: string;
  private maxTimer?: ReturnType<typeof setTimeout>;
  private monitor?: ReturnType<typeof setInterval>;
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
    if (this.capturing)
      return this.captureLimited
        ? "Clip limit — release to send"
        : this.captureReady
          ? "Recording — release to send"
          : "Starting microphone…";
    if (this.textOnly) return "Text conversation connected";
    if (this.mode === "speaking") return "Speaking";
    if (this.mode === "synthesizing") return "Preparing speech";
    return this.micMuted ? "Microphone muted" : "Ready for push-to-talk";
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
    if (
      this.grant &&
      this.textOnly === textOnly &&
      (textOnly || this.stream?.active)
    )
      return;
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
    clearInterval(this.monitor);
    this.stream?.getTracks().forEach((track) => track.stop());
    void this.context?.close();
    this.stream = undefined;
    this.context = undefined;
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
    this.samples = new Float32Array(analyser.fftSize);
    const mime = ["audio/webm", "audio/mp4", "audio/ogg"].find((type) =>
      MediaRecorder.isTypeSupported(type),
    );
    if (!mime)
      throw new Error(
        "This browser cannot record a supported audio format. Use typed chat.",
      );
    this.mime = mime;
    this.monitor = setInterval(() => {
      if (
        !this.current(generation) ||
        this.micMuted ||
        this.pending ||
        this.mode !== "speaking"
      )
        return;
      const samples = this.samples;
      if (!samples) return;
      analyser.getFloatTimeDomainData(samples);
      const rms = Math.sqrt(
        samples.reduce((sum, value) => sum + value * value, 0) / samples.length,
      );
      if (rms > 0.025) this.interrupt();
    }, 100);
  }
  /** Press-and-hold capture; the existing MediaRecorder/WAV upload path is unchanged. */
  async beginPushToTalk() {
    if (this.closed || this.capture || this.pending || this.micMuted) return;
    if (this.status === "connecting") return;
    const generation = this.generation;
    this.error = "";
    this.notice = "";
    this.interrupt();
    this.capturing = true;
    this.captureReady = false;
    this.captureLimited = false;
    this.update();
    try {
      if (
        this.status !== "connected" ||
        this.textOnly ||
        !this.stream?.active ||
        !this.mime
      )
        await this.start(false);
      if (!this.capturing || !this.current(generation)) return;
      if (
        this.status !== "connected" ||
        this.textOnly ||
        !this.stream?.active ||
        !this.mime
      ) {
        this.capturing = false;
        this.update();
        return;
      }
      const recorder = new MediaRecorder(this.stream, { mimeType: this.mime });
      const capture: Capture = {
        recorder,
        chunks: [],
        generation,
        finished: false,
        stopped: false,
      };
      this.capture = capture;
      recorder.onstart = () => {
        if (this.capture !== capture || capture.release) return;
        this.captureReady = true;
        this.update();
      };
      recorder.ondataavailable = (event) => {
        if (event.data.size) capture.chunks.push(event.data);
      };
      recorder.onstop = () => {
        capture.stopped = true;
        // At the duration limit, retain the bounded clip until the gesture
        // chooses send or cancel. Never send merely because a hold timed out.
        if (this.captureLimited && !capture.release) {
          this.update();
          return;
        }
        this.finishCapture(capture);
      };
      recorder.onerror = () => {
        capture.release = "cancel";
        this.finishCapture(capture);
        if (this.current(generation))
          this.fail(
            new Error(
              "Microphone recording failed. Hold to retry or use typed chat.",
            ),
          );
      };
      recorder.start(100);
      this.maxTimer = setTimeout(() => {
        if (this.capture !== capture || capture.release) return;
        this.captureLimited = true;
        if (recorder.state !== "inactive") recorder.stop();
        this.update();
      }, 15000);
      this.update();
    } catch (error) {
      this.capture = undefined;
      this.capturing = false;
      this.clearCaptureTimers();
      if (this.current(generation)) this.fail(error);
    }
  }
  endPushToTalk() {
    this.releasePushToTalk("send");
  }
  cancelPushToTalk() {
    this.releasePushToTalk("cancel");
  }
  private releasePushToTalk(release: "send" | "cancel") {
    if (!this.capturing) return;
    const capture = this.capture;
    if (!capture) {
      this.capturing = false;
      if (release === "send")
        this.notice =
          "The clip was too short to send. Hold the button while you speak.";
      this.update();
      return;
    }
    if (capture.release) return;
    capture.release = release;
    this.clearCaptureTimers();
    if (capture.stopped) this.finishCapture(capture);
    else if (capture.recorder.state !== "inactive") capture.recorder.stop();
    this.update();
  }
  private finishCapture(capture: Capture) {
    if (capture.finished) return;
    capture.finished = true;
    this.clearCaptureTimers();
    if (this.capture === capture) this.capture = undefined;
    this.capturing = false;
    this.captureReady = false;
    this.captureLimited = false;
    this.update();
    if (
      capture.release !== "send" ||
      !this.current(capture.generation) ||
      this.pending
    )
      return;
    if (!capture.chunks.length) {
      this.notice = "No audio was captured. Hold the button and speak.";
      this.update();
      return;
    }
    const type = capture.chunks[0]?.type || this.mime;
    void this.transcribe(
      new Blob(capture.chunks, { type }),
      capture.generation,
    );
  }
  private clearCaptureTimers() {
    if (this.maxTimer) clearTimeout(this.maxTimer);
    this.maxTimer = undefined;
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
    this.cancelPushToTalk();
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
      audio.onerror = () => finish(new Error("Could not play speech audio"));
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
    if (this.micMuted) this.cancelPushToTalk();
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
    clearInterval(this.monitor);
    this.releasePushToTalk("cancel");
    this.capturing = false;
    this.notice = "";
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = undefined;
    this.samples = undefined;
    this.mime = undefined;
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
