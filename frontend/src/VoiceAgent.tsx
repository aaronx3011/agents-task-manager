import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Mic, MicOff, Send, Square, Volume2, VolumeX } from "lucide-react";
import { api, unwrap } from "./api-client";
import { PMConversation } from "./voice-client";
import type { PMStatus } from "./voice-client";
import { refreshTaskViews } from "./task-sync";
import { HoldToTalk, pointerWithinRect } from "./voice-hold";
import "./voice-agent.css";

export default function VoiceAgent() {
  const [, render] = useState(0);
  const [draft, setDraft] = useState("");
  const [client, setClient] = useState<PMConversation>();
  const transcript = useRef<HTMLDivElement>(null);
  const hold = useRef(new HoldToTalk());
  const queries = useQueryClient();
  useEffect(() => {
    const connection = new PMConversation(
      () => render((value) => value + 1),
      () => {
        void refreshTaskViews(queries);
      },
    );
    setClient(connection);
    return () => connection.close();
  }, [queries]);
  const status = useQuery({
    queryKey: ["voice-status"],
    queryFn: async ({ signal }) =>
      (await unwrap(api.GET("/api/v1/voice/status", { signal }))) as PMStatus,
    refetchInterval: 30000,
    retry: false,
  });
  useEffect(() => {
    transcript.current?.scrollTo({ top: transcript.current.scrollHeight });
  });
  const connected = client?.status === "connected";
  const busy = client?.status === "connecting" || Boolean(client?.pending);
  const ready = Boolean(status.data?.configured);
  const recording = Boolean(client?.capturing);
  const cancelHold = () => {
    hold.current.cancel();
    client?.cancelPushToTalk();
  };
  useEffect(() => {
    const cancel = () => {
      hold.current.cancel();
      client?.cancelPushToTalk();
    };
    const visibility = () => {
      if (document.hidden) cancel();
    };
    window.addEventListener("blur", cancel);
    document.addEventListener("visibilitychange", visibility);
    return () => {
      window.removeEventListener("blur", cancel);
      document.removeEventListener("visibilitychange", visibility);
    };
  }, [client]);
  return (
    <section className="voice-agent" aria-label="Voice agent">
      <header className="voice-heading">
        <div>
          <h1>
            <Mic size={23} /> PM router
          </h1>
          <p>
            Talk naturally about projects, create tasks, and check progress.
          </p>
        </div>
        <button
          className="secondary-button"
          onClick={() => void client?.clear()}
        >
          Clear conversation
        </button>
      </header>
      <div className="voice-services" aria-label="Agent services">
        <span data-ready={ready}>
          Fireworks reasoning ·{" "}
          {status.isPending ? "checking" : ready ? "configured" : "unavailable"}
        </span>
        <span data-ready={status.data?.transcription_configured}>
          ElevenLabs transcription ·{" "}
          {status.data?.transcription_configured ? "configured" : "unavailable"}
        </span>
        <span data-ready={status.data?.tts_configured}>
          ElevenLabs TTS ·{" "}
          {status.data?.tts_configured ? "configured" : "unavailable"}
        </span>
        <span data-ready={connected}>
          PM agent ·{" "}
          {connected ? "connected" : busy ? "connecting" : "not connected"}
        </span>
        <button className="text-button" onClick={() => void status.refetch()}>
          Check connection
        </button>
      </div>
      {status.data?.limits && (
        <p className="muted">
          Conversation limits:{" "}
          {status.data.limits.max_messages.toLocaleString()} messages
          {" · "}
          {status.data.limits.max_history_bytes.toLocaleString()} history bytes
          {" · "}
          {status.data.limits.max_output_tokens.toLocaleString()} output tokens
          {" · "}
          {status.data.limits.context_window_tokens.toLocaleString()} context
          tokens
          {
            " (conservative budget). History is preserved when a limit is reached."
          }
        </p>
      )}
      {(status.isError || client?.error || (!status.isPending && !ready)) && (
        <div className="voice-error" role="alert">
          {client?.error ||
            status.error?.message ||
            "Fireworks PM reasoning is not configured on the backend yet."}
        </div>
      )}
      <div
        className="voice-transcript"
        ref={transcript}
        role="log"
        aria-label="Conversation transcript"
        aria-live="polite"
      >
        {!client?.entries.length && (
          <div className="voice-welcome">
            <Mic size={40} />
            <h2>What would you like to work on?</h2>
            <p>
              Start a voice conversation, or type to chat without microphone
              access.
            </p>
            <div>
              {[
                "What projects need my attention?",
                "What tasks are pending in agents-task-manager?",
              ].map((text) => (
                <button
                  className="secondary-button"
                  key={text}
                  disabled={!ready || busy}
                  onClick={() => void client?.send(text)}
                >
                  {text}
                </button>
              ))}
            </div>
          </div>
        )}
        {client?.entries.map((entry, index) => (
          <article key={index} className={`voice-entry voice-${entry.role}`}>
            <small>
              {entry.role === "user"
                ? "You"
                : entry.role === "tool"
                  ? "Task Manager tool"
                  : "PM agent"}
            </small>
            <p>{entry.text || "…"}</p>
          </article>
        ))}
      </div>
      <footer className="voice-controls">
        <div className="voice-buttons">
          <button
            className="primary-button"
            disabled={
              !ready ||
              !status.data?.speech_configured ||
              busy ||
              (connected && !client?.textOnly)
            }
            onClick={() => void client?.start(false)}
          >
            <Mic size={17} /> Start voice conversation
          </button>
          <button
            className="secondary-button"
            disabled={!connected && !busy}
            onClick={() => void client?.stop()}
          >
            <Square size={15} /> End conversation
          </button>
          <button
            type="button"
            className="primary-button voice-hold"
            aria-label="Push to talk"
            aria-pressed={recording}
            data-capture-ready={Boolean(client?.captureReady)}
            aria-describedby="voice-hold-help"
            data-state={
              recording
                ? "recording"
                : busy
                  ? "processing"
                  : client?.error
                    ? "error"
                    : "idle"
            }
            disabled={
              !ready ||
              !status.data?.speech_configured ||
              Boolean(client?.micMuted) ||
              (busy && !recording)
            }
            onPointerDown={(event) => {
              if (
                event.button !== 0 ||
                !event.isPrimary ||
                !hold.current.beginPointer(event.pointerId)
              )
                return;
              event.preventDefault();
              event.currentTarget.focus({ preventScroll: true });
              event.currentTarget.setPointerCapture(event.pointerId);
              void client?.beginPushToTalk();
            }}
            onPointerUp={(event) => {
              const outcome = hold.current.endPointer(
                event.pointerId,
                pointerWithinRect(
                  event.currentTarget.getBoundingClientRect(),
                  event.clientX,
                  event.clientY,
                ),
              );
              if (outcome === "none") return;
              event.preventDefault();
              if (event.currentTarget.hasPointerCapture(event.pointerId))
                event.currentTarget.releasePointerCapture(event.pointerId);
              if (outcome === "send") client?.endPushToTalk();
              else client?.cancelPushToTalk();
            }}
            onPointerCancel={cancelHold}
            onLostPointerCapture={() => {
              if (hold.current.active) cancelHold();
            }}
            onBlur={cancelHold}
            onContextMenu={(event) => event.preventDefault()}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                cancelHold();
              }
              if (event.key !== " " && event.key !== "Enter") return;
              event.preventDefault();
              if (!event.repeat && hold.current.beginKeyboard())
                void client?.beginPushToTalk();
            }}
            onKeyUp={(event) => {
              if (event.key !== " " && event.key !== "Enter") return;
              event.preventDefault();
              if (hold.current.endKeyboard() === "send")
                client?.endPushToTalk();
            }}
          >
            <Mic size={17} />{" "}
            {recording
              ? client?.captureLimited
                ? "Limit reached — release"
                : client?.captureReady
                  ? "Recording — release to send"
                  : "Starting microphone…"
              : busy
                ? "Processing…"
                : client?.error
                  ? "Hold to retry"
                  : "Hold to talk"}
          </button>
          <button
            type="button"
            className="secondary-button"
            style={{ visibility: recording ? "visible" : "hidden" }}
            disabled={!recording}
            onClick={cancelHold}
          >
            Cancel recording
          </button>
          <button
            className="icon-button"
            style={{
              visibility: connected && !client?.textOnly ? "visible" : "hidden",
            }}
            disabled={!connected || client?.textOnly}
            aria-label={
              client?.micMuted ? "Unmute microphone" : "Mute microphone"
            }
            onClick={() => client?.toggleMic()}
          >
            {client?.micMuted ? <MicOff size={18} /> : <Mic size={18} />}
          </button>
          <button
            className="icon-button"
            aria-label={
              client?.muted ? "Enable spoken replies" : "Mute spoken replies"
            }
            aria-pressed={Boolean(client?.muted)}
            onClick={() => client?.setMuted(!client.muted)}
          >
            {client?.muted ? <VolumeX size={18} /> : <Volume2 size={18} />}
          </button>
          <span role="status">{client?.state || "Ready to talk"}</span>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (draft.trim()) {
              void client?.send(draft);
              setDraft("");
            }
          }}
        >
          <input
            aria-label="Message PM agent"
            placeholder="Ask about projects or describe a task…"
            value={draft}
            maxLength={4000}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button
            className="primary-button"
            aria-label="Send message"
            disabled={!ready || busy || !draft.trim()}
          >
            <Send size={17} /> Send
          </button>
        </form>
        {client?.notice && <p role="status">{client.notice}</p>}
        <p id="voice-hold-help" className="voice-footnote">
          Hold the button, Space or Enter to talk; release to send. Release
          outside or press Escape to discard.
        </p>
        <p className="voice-footnote">
          Fireworks reasoning · ElevenLabs transcription and speech · Speak to
          interrupt with push-to-talk · Task changes are handled by Task Manager
        </p>
      </footer>
    </section>
  );
}
