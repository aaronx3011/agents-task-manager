import { Conversation } from "@elevenlabs/client";
import type {
  Conversation as ConversationInstance,
  PartialOptions,
  Status,
} from "@elevenlabs/client";
import { ApiError, api, unwrap } from "./api-client";

export type PMEntry = {
  role: "user" | "assistant" | "tool";
  text: string;
  event?: number;
};
export type PMStatus = {
  provider: string;
  configured: boolean;
  policy_version: string;
};
type Grant = { id: string; signed_url: string; tool_token: string };
type Connector = (options: PartialOptions) => Promise<ConversationInstance>;

export class PMConversation {
  entries: PMEntry[] = [];
  status: Status = "disconnected";
  mode = "listening";
  textOnly = true;
  muted = false;
  micMuted = false;
  error = "";
  private connection?: ConversationInstance;
  private grant?: Grant;
  private generation = 0;
  private closed = false;
  private starting?: Promise<void>;
  private toolCalls = new Set<string>();
  private streamIndex?: number;
  constructor(
    privateChanged: () => void,
    privateToolUsed: () => void,
    connector: Connector = (options) => Conversation.startSession(options),
  ) {
    this.changed = privateChanged;
    this.toolUsed = privateToolUsed;
    this.connector = connector;
  }
  private changed: () => void;
  private toolUsed: () => void;
  private connector: Connector;
  update() {
    if (!this.closed) this.changed();
  }
  get state() {
    return this.status === "connecting"
      ? "Connecting to PM…"
      : this.status !== "connected"
        ? "Ready to talk"
        : this.textOnly
          ? "Text conversation connected"
          : this.mode === "speaking"
            ? "Speaking"
            : this.micMuted
              ? "Microphone muted"
              : "Listening";
  }
  private async revoke(grant?: Grant) {
    if (grant)
      await api
        .DELETE("/api/v1/voice/sessions/{id}", {
          params: { path: { id: grant.id } },
        })
        .catch(() => {});
  }
  async start(textOnly: boolean) {
    if (this.closed) return;
    if (this.starting) return this.starting;
    if (this.connection && this.textOnly === textOnly) return;
    if (this.connection) await this.stop();
    const generation = ++this.generation;
    this.status = "connecting";
    this.error = "";
    this.textOnly = textOnly;
    this.update();
    this.starting = (async () => {
      let grant: Grant | undefined;
      try {
        await unwrap(api.GET("/api/v1/auth/csrf"));
        grant = await unwrap(
          api.POST("/api/v1/voice/sessions", { body: { text_only: textOnly } }),
        );
        if (this.generation !== generation || this.closed) {
          await this.revoke(grant);
          return;
        }
        this.grant = grant;
        const connection = await this.connector({
          signedUrl: grant.signed_url,
          connectionType: "websocket",
          textOnly,
          overrides: { conversation: { textOnly } },
          dynamicVariables: { secret__atm_voice_token: grant.tool_token },
          onStatusChange: ({ status }) => {
            if (generation === this.generation) {
              this.status = status;
              this.update();
            }
          },
          onModeChange: ({ mode }) => {
            if (generation === this.generation) {
              this.mode = mode;
              this.update();
            }
          },
          onMessage: ({ role, message, event_id }) => {
            if (generation !== this.generation || this.closed) return;
            if (role === "agent" && this.streamIndex !== undefined) {
              this.entries[this.streamIndex] = {
                role: "assistant",
                text: message,
                event: event_id,
              };
              this.streamIndex = undefined;
            } else if (
              role !== "user" ||
              this.entries.at(-1)?.role !== "user" ||
              this.entries.at(-1)?.text !== message
            ) {
              const existing = this.entries.find(
                (entry) =>
                  entry.event === event_id &&
                  entry.role === (role === "agent" ? "assistant" : "user"),
              );
              if (existing) existing.text = message;
              else
                this.entries.push({
                  role: role === "agent" ? "assistant" : "user",
                  text: message,
                  event: event_id,
                });
            }
            if (role === "agent") this.toolUsed();
            this.update();
          },
          onAgentChatResponsePart: (part) => {
            if (generation !== this.generation || this.closed) return;
            if (part.type === "start") {
              this.streamIndex = this.entries.length;
              this.entries.push({ role: "assistant", text: "" });
            }
            if (part.type === "delta" && this.streamIndex !== undefined)
              this.entries[this.streamIndex].text += part.text;
            this.update();
          },
          onAgentToolResponse: (result) => {
            if (
              generation !== this.generation ||
              this.closed ||
              this.toolCalls.has(result.tool_call_id)
            )
              return;
            this.toolCalls.add(result.tool_call_id);
            this.entries.push({
              role: "tool",
              text: `${result.tool_name} · ${result.is_error ? "reported an error" : "result received"}`,
            });
            this.toolUsed();
            this.update();
          },
          onError: () => {
            if (generation === this.generation) {
              this.error =
                "ElevenLabs conversation failed. Reconnect to try again; check task state before repeating a write.";
              this.update();
            }
          },
          onDisconnect: () => {
            if (generation !== this.generation) return;
            this.connection = undefined;
            this.grant = undefined;
            this.status = "disconnected";
            void this.revoke(grant);
            this.update();
          },
        });
        if (this.closed || this.generation !== generation) {
          await connection.endSession();
          await this.revoke(grant);
          return;
        }
        this.connection = connection;
        this.status = "connected";
        connection.setVolume({ volume: this.muted ? 0 : 1 });
        this.update();
      } catch (error) {
        await this.revoke(grant);
        if (generation === this.generation && !this.closed) {
          this.status = "disconnected";
          this.grant = undefined;
          this.error =
            error instanceof ApiError
              ? error.message
              : "Could not connect to ElevenLabs. Check access and microphone permission, then reconnect.";
          this.update();
        }
      } finally {
        this.starting = undefined;
      }
    })();
    return this.starting;
  }
  async send(text: string) {
    if (this.closed || !text.trim()) return;
    if (!this.connection) await this.start(true);
    if (!this.connection || this.closed) return;
    this.entries.push({ role: "user", text: text.trim() });
    this.connection.sendUserMessage(text.trim());
    this.update();
  }
  setMuted(muted: boolean) {
    this.muted = muted;
    this.connection?.setVolume({ volume: muted ? 0 : 1 });
    this.update();
  }
  toggleMic() {
    this.micMuted = !this.micMuted;
    this.connection?.setMicMuted(this.micMuted);
    this.update();
  }
  async stop() {
    ++this.generation;
    const connection = this.connection,
      grant = this.grant;
    this.connection = undefined;
    this.grant = undefined;
    this.status = "disconnected";
    this.streamIndex = undefined;
    this.micMuted = false;
    this.update();
    try {
      await connection?.endSession();
    } finally {
      await this.revoke(grant);
    }
  }
  async clear() {
    await this.stop();
    this.entries = [];
    this.toolCalls.clear();
    this.error = "";
    this.update();
  }
  close() {
    this.closed = true;
    void this.stop();
  }
}
