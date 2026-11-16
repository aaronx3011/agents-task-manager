import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Mic, MicOff, Send, Square, Volume2, VolumeX } from "lucide-react";
import { api, unwrap } from "./api-client";
import { PMConversation } from "./eleven-client";
import type { PMStatus } from "./eleven-client";
import { refreshTaskViews } from "./task-sync";
import "./voice-agent.css";

export default function VoiceAgent() {
  const [, render] = useState(0);
  const [draft, setDraft] = useState("");
  const [client, setClient] = useState<PMConversation>();
  const transcript = useRef<HTMLDivElement>(null);
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
  const busy = client?.status === "connecting";
  const ready = Boolean(status.data?.configured);
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
          ElevenLabs ·{" "}
          {status.isPending ? "checking" : ready ? "configured" : "unavailable"}
        </span>
        <span data-ready={connected}>
          PM agent ·{" "}
          {connected ? "connected" : busy ? "connecting" : "not connected"}
        </span>
        <button className="text-button" onClick={() => void status.refetch()}>
          Check connection
        </button>
      </div>
      {(status.isError || client?.error || (!status.isPending && !ready)) && (
        <div className="voice-error" role="alert">
          {client?.error ||
            status.error?.message ||
            "The ElevenLabs PM agent is not configured on the backend yet."}
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
            disabled={!ready || busy || (connected && !client?.textOnly)}
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
          {connected && !client?.textOnly && (
            <button
              className="icon-button"
              aria-label={
                client?.micMuted ? "Unmute microphone" : "Mute microphone"
              }
              onClick={() => client?.toggleMic()}
            >
              {client?.micMuted ? <MicOff size={18} /> : <Mic size={18} />}
            </button>
          )}
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
        <p className="voice-footnote">
          Powered by ElevenLabs · Starting voice from text begins a new agent
          session · Speak to interrupt · Task changes are handled by Task
          Manager
        </p>
      </footer>
    </section>
  );
}
