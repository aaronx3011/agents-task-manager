// The PM router conversation for the mobile app, mirroring the web's
// voice-client.ts. The server owns history and reasoning; this client holds a
// short-lived conversation grant, captures push-to-talk clips as 24 kHz WAV,
// and plays spoken replies.
import {
  createAudioPlayer,
  getRecordingPermissionsAsync,
  requestRecordingPermissionsAsync,
  setAudioModeAsync,
} from "expo-audio";
import type { AudioStream, AudioStreamBuffer } from "expo-audio";
import { File, Paths } from "expo-file-system";
import { unwrap } from "@/api/client";
import type { Api, Schemas } from "@/api/client";
import {
  MAX_CLIP_SECONDS,
  monoSamples,
  playSpeech,
  stopIfAborted,
  WAV_RATE,
  wavBytes,
} from "@/voice/audio";
import type { PcmBuffer } from "@/voice/audio";

export type PMStatus = Schemas["VoiceStatusOutput"];
export interface Entry {
  role: "user" | "assistant" | "tool";
  text: string;
}
type Grant = { id: string; tool_token: string };
type Capture = {
  buffers: PcmBuffer[];
  generation: number;
  release?: "send" | "cancel";
  started: boolean;
  finished: boolean;
  unsubscribe?: () => void;
};

/** Clips shorter than this are almost always an accidental tap. */
const MIN_CLIP_SECONDS = 0.3;

function cacheFile(extension: string) {
  return new File(
    Paths.cache,
    `pm-voice-${Date.now()}-${Math.random().toString(36).slice(2)}.${extension}`,
  );
}

function discard(file: File) {
  try {
    if (file.exists) file.delete();
  } catch {
    // A leftover cache file is harmless; the system may purge the cache.
  }
}

export class PMConversation {
  entries: Entry[] = [];
  status: "disconnected" | "connecting" | "connected" = "disconnected";
  mode: "listening" | "transcribing" | "thinking" | "synthesizing" | "speaking" = "listening";
  /** Replies are spoken once the person has used their voice in this conversation. */
  spoken = false;
  muted = false;
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
  private capture?: Capture;
  private maxTimer?: ReturnType<typeof setTimeout>;
  private speechController?: AbortController;
  private starting?: Promise<void>;
  private askingPermission = false;

  constructor(
    private api: Api,
    private stream: AudioStream,
    private changed: () => void,
    private toolUsed: () => void,
  ) {}

  private update() {
    if (!this.closed) this.changed();
  }

  get state() {
    if (this.status === "connecting") return "Connecting to PM…";
    if (this.pending) return this.mode === "transcribing" ? "Transcribing" : "Thinking";
    if (this.capturing)
      return this.captureLimited
        ? "Clip limit — release to send"
        : this.captureReady
          ? "Recording — release to send"
          : "Starting microphone…";
    if (this.mode === "speaking") return "Speaking";
    if (this.mode === "synthesizing") return "Preparing speech";
    if (this.status !== "connected") return "Ready to talk";
    return this.spoken ? "Ready for push-to-talk" : "Text conversation connected";
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

  /** Obtain a conversation grant; the server keeps the history across turns. */
  private async start() {
    if (this.closed || this.grant) return;
    if (this.starting) return this.starting;
    const generation = this.generation;
    this.starting = (async () => {
      this.status = "connecting";
      this.error = "";
      this.update();
      try {
        const grant = await unwrap(
          this.api.POST("/api/v1/voice/sessions", {
            body: { text_only: !this.spoken },
            signal: this.controller.signal,
          }),
        );
        if (!this.current(generation)) {
          await this.revoke(grant);
          return;
        }
        this.grant = grant;
        this.entries = [];
        this.status = "connected";
      } catch (error) {
        if (this.current(generation)) {
          this.status = "disconnected";
          this.fail(error);
        }
      } finally {
        this.starting = undefined;
        this.update();
      }
    })();
    return this.starting;
  }

  /** Press-and-hold capture. Holding to talk also interrupts a spoken reply. */
  async beginPushToTalk() {
    if (this.closed || this.capture || this.pending || this.status === "connecting") return;
    const generation = this.generation;
    const capture: Capture = { buffers: [], generation, started: false, finished: false };
    this.capture = capture;
    this.error = "";
    this.notice = "";
    this.interrupt();
    this.capturing = true;
    this.captureReady = false;
    this.captureLimited = false;
    this.update();
    try {
      const permission = await this.microphonePermission();
      if (!permission.granted)
        throw new Error(
          "Microphone access is off. Allow it in Settings, or type to chat with the PM agent.",
        );
      this.spoken = true;
      await this.start();
      if (capture.release || !this.current(generation) || !this.grant) {
        this.finishCapture(capture);
        return;
      }
      await setAudioModeAsync({ allowsRecording: true, playsInSilentMode: true });
      const subscription = this.stream.addListener(
        "audioStreamBuffer",
        (buffer: AudioStreamBuffer) => {
          if (!capture.finished && !this.captureLimited) capture.buffers.push(buffer);
        },
      );
      capture.unsubscribe = () => subscription.remove();
      await this.stream.start();
      capture.started = true;
      if (capture.release || !this.current(generation)) {
        this.finishCapture(capture);
        return;
      }
      this.captureReady = true;
      this.maxTimer = setTimeout(() => {
        if (this.capture !== capture || capture.release) return;
        // Keep the bounded clip until the gesture chooses send or cancel.
        this.captureLimited = true;
        this.stopStream(capture);
        this.update();
      }, MAX_CLIP_SECONDS * 1000);
      this.update();
    } catch (error) {
      capture.release = "cancel";
      this.finishCapture(capture);
      if (this.current(generation)) this.fail(error);
    }
  }

  /**
   * Ask only when not yet granted: on Android the request opens a system
   * activity that briefly backgrounds the app even when access exists.
   */
  private async microphonePermission() {
    const current = await getRecordingPermissionsAsync();
    if (current.granted) return current;
    this.askingPermission = true;
    try {
      return await requestRecordingPermissionsAsync();
    } finally {
      this.askingPermission = false;
    }
  }

  /** Leaving the app discards a clip in progress, but not the permission prompt. */
  appStateChanged(state: string) {
    if (state === "background" && !this.askingPermission) this.cancelPushToTalk();
  }

  endPushToTalk() {
    this.releasePushToTalk("send");
  }

  cancelPushToTalk() {
    this.releasePushToTalk("cancel");
  }

  private releasePushToTalk(release: "send" | "cancel") {
    const capture = this.capture;
    if (!capture || capture.release) return;
    capture.release = release;
    // Still asking for permission or starting the microphone: beginPushToTalk
    // sees the release and finishes the capture once the stream is up.
    if (capture.started) this.finishCapture(capture);
    else this.update();
  }

  private stopStream(capture: Capture) {
    capture.unsubscribe?.();
    capture.unsubscribe = undefined;
    if (capture.started) {
      try {
        this.stream.stop();
      } catch {
        // Already stopped.
      }
    }
  }

  private finishCapture(capture: Capture) {
    if (capture.finished) return;
    capture.finished = true;
    this.stopStream(capture);
    this.clearCaptureTimer();
    if (this.capture === capture) this.capture = undefined;
    this.capturing = false;
    this.captureReady = false;
    this.captureLimited = false;
    // Leave record mode so replies use the loudspeaker on iOS.
    void setAudioModeAsync({ allowsRecording: false, playsInSilentMode: true }).catch(() => {});
    this.update();
    if (capture.release !== "send" || !this.current(capture.generation) || this.pending) return;
    const samples = monoSamples(capture.buffers);
    if (samples.length < WAV_RATE * MIN_CLIP_SECONDS) {
      this.notice = capture.buffers.length
        ? "The clip was too short to send. Hold the button while you speak."
        : "No audio was captured. Hold the button and speak.";
      this.update();
      return;
    }
    void this.transcribe(
      wavBytes(samples.subarray(0, WAV_RATE * MAX_CLIP_SECONDS)),
      capture.generation,
    );
  }

  private clearCaptureTimer() {
    if (this.maxTimer) clearTimeout(this.maxTimer);
    this.maxTimer = undefined;
  }

  private async transcribe(wav: Uint8Array, generation: number) {
    this.pending = true;
    this.mode = "transcribing";
    this.update();
    try {
      // Expo's global fetch encodes multipart parts from name/type/bytes();
      // React Native's { uri } file parts are rejected before any request is sent.
      const form = new FormData();
      form.append("file", {
        name: "recording.wav",
        type: "audio/wav",
        bytes: async () => wav,
      } as unknown as Blob);
      const result = await unwrap(
        this.api.POST("/api/v1/voice/transcriptions", {
          ...this.options(),
          body: { file: "recording" },
          bodySerializer: () => form,
        }),
      );
      if (this.current(generation) && result.text.trim()) await this.turn(result.text, generation);
      else if (this.current(generation)) this.notice = "No speech was recognised. Try again.";
    } catch (error) {
      if (this.current(generation)) this.fail(error);
    } finally {
      if (this.current(generation)) {
        this.pending = false;
        if (this.mode === "transcribing" || this.mode === "thinking") this.mode = "listening";
        this.update();
      }
    }
  }

  async send(text: string) {
    if (this.closed || this.pending || !text.trim()) return;
    await this.start();
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
        if (this.mode === "thinking") this.mode = "listening";
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
      this.api.POST("/api/v1/voice/chat", { ...this.options(), body: { text } }),
    );
    if (!this.current(generation)) return;
    reply.events.forEach((event) =>
      this.entries.push({
        role: "tool",
        text: `${event.tool} · ${event.is_error ? "reported an error" : "result received"}`,
      }),
    );
    this.entries.push({ role: "assistant", text: reply.text });
    this.mode = "listening";
    this.toolUsed();
    this.update();
    if (this.spoken && !this.muted) void this.speak(reply.text, generation);
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
            this.api.POST("/api/v1/voice/speech", {
              ...this.options(),
              signal,
              body: { text: chunk },
              parseAs: "arrayBuffer",
            }),
          ) as Promise<ArrayBuffer>,
        (audio, signal) => this.play(audio, signal),
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

  private async play(audio: ArrayBuffer, signal: AbortSignal): Promise<void> {
    stopIfAborted(signal);
    const file = cacheFile("mp3");
    file.create({ overwrite: true });
    try {
      await file.write(new Uint8Array(audio));
      stopIfAborted(signal);
    } catch (error) {
      discard(file);
      throw error;
    }
    const player = createAudioPlayer({ uri: file.uri });
    return new Promise<void>((resolve, reject) => {
      let settled = false;
      const finish = (error?: unknown) => {
        if (settled) return;
        settled = true;
        subscription.remove();
        signal.removeEventListener("abort", abort);
        try {
          player.pause();
          player.remove();
        } catch {
          // Already released.
        }
        discard(file);
        if (error) reject(error);
        else resolve();
      };
      const abort = () => finish(new Error("Stopped"));
      const subscription = player.addListener("playbackStatusUpdate", (status) => {
        if (status.didJustFinish) finish();
        else if (status.error) finish(new Error("Could not play speech audio"));
      });
      signal.addEventListener("abort", abort);
      this.mode = "speaking";
      this.update();
      player.play();
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

  private async revoke(grant?: Grant) {
    if (grant)
      await this.api
        .DELETE("/api/v1/voice/sessions/{id}", { params: { path: { id: grant.id } } })
        .catch(() => {});
  }

  /** End the conversation on the server; the transcript stays until cleared. */
  async stop() {
    ++this.generation;
    this.controller.abort();
    this.controller = new AbortController();
    this.cancelPushToTalk();
    this.notice = "";
    this.interrupt();
    const grant = this.grant;
    this.grant = undefined;
    this.status = "disconnected";
    this.pending = false;
    this.spoken = false;
    this.mode = "listening";
    this.update();
    await this.revoke(grant);
  }

  async clear() {
    await this.stop();
    this.entries = [];
    this.error = "";
    this.update();
  }

  /** Resume after close() (React may remount the same client). */
  open() {
    this.closed = false;
  }

  /** Stop and end the server conversation; later updates are ignored. */
  close() {
    void this.stop();
    this.closed = true;
  }
}
