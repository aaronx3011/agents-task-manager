import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Mic, MicOff, Send, Square, Volume2, VolumeX } from "lucide-react";
import { VOICE_API, VoiceClient } from "./voice-client";
import type { VoiceStatus } from "./voice-client";
import { refreshTaskViews } from "./task-sync";
import "./voice-agent.css";

export default function VoiceAgent() {
  const [, render] = useState(0);
  const [draft, setDraft] = useState("");
  const [client, setClient] = useState<VoiceClient>();
  const transcript = useRef<HTMLDivElement>(null);
  const queries = useQueryClient();
  useEffect(() => {
    const connection = new VoiceClient(
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
    queryFn: async ({ signal }): Promise<VoiceStatus> => {
      const response = await fetch(VOICE_API + "/status", { signal });
      if (
        !response.ok ||
        !response.headers.get("content-type")?.includes("application/json")
      )
        throw new Error(
          "Voice Lab is unavailable. Check the voice service and its web proxy.",
        );
      return response.json();
    },
    refetchInterval: 15000,
    retry: false,
  });
  useEffect(() => {
    transcript.current?.scrollTo({ top: transcript.current.scrollHeight });
  });
  const ready = status.data;
  const send = async (text: string) => {
    if (!client || !text.trim()) return;
    // Unlock audio within the user's gesture; typed chat remains usable when
    // speech is offline or the browser does not support AudioContext.
    try {
      if (ready?.speech) await client.unlockAudio();
    } catch {
      /* text fallback */
    }
    void client.send(text);
  };
  return (
    <section className="voice-agent" aria-label="Voice agent">
      <header className="voice-heading">
        <div>
          <h1>
            <Mic size={23} /> Voice agent
          </h1>
          <p>
            Talk to your existing agent to create tasks, check progress, and
            recall project context.
          </p>
        </div>
        <button
          className="secondary-button"
          onClick={() => {
            client?.stopReply();
            client?.stopMic();
            if (client) {
              client.entries = [];
              client.messages = [];
              client.error = "";
              client.update();
            }
          }}
        >
          Clear conversation
        </button>
      </header>
      <div className="voice-services" aria-label="Agent services">
        {[
          ["Speech", ready?.speech],
          [ready?.model || "Language model", ready?.modelReady],
          ["Tasks", ready?.mcp?.["task-coordinator"]?.connected],
          ["Memory", ready?.mcp?.["agent-memory"]?.connected],
          ["PM router", ready?.policy?.version?.startsWith("pm-router-")],
        ].map(([name, connected]) => (
          <span key={String(name)} data-ready={Boolean(connected)}>
            {String(name)} ·{" "}
            {status.isPending
              ? "checking"
              : connected
                ? "ready"
                : "unavailable"}
          </span>
        ))}
        <button className="text-button" onClick={() => void status.refetch()}>
          Reconnect / check
        </button>
      </div>
      {(status.isError || client?.error) && (
        <div className="voice-error" role="alert">
          {client?.error || status.error?.message}
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
              Start the microphone or type a message. Mention the project when
              creating a task.
            </p>
            <div>
              {[
                "What tasks are ready in agents-task-manager?",
                "Tell me the status of task 296.",
              ].map((text) => (
                <button
                  key={text}
                  className="secondary-button"
                  onClick={() => void send(text)}
                  disabled={!ready?.modelReady}
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
                  ? "Agent tool"
                  : "Assistant"}
            </small>
            <p>{entry.text || "…"}</p>
          </article>
        ))}
      </div>
      <footer className="voice-controls">
        <div className="voice-buttons">
          <button
            className={client?.capture ? "primary-button" : "secondary-button"}
            disabled={!client || (!client.capture && !ready?.speech)}
            onClick={() =>
              client?.capture ? client.stopMic() : void client?.startMic()
            }
          >
            {client?.capture ? <MicOff size={17} /> : <Mic size={17} />}
            {client?.capture ? "Stop microphone" : "Start microphone"}
          </button>
          <button
            className="secondary-button"
            disabled={!client?.active}
            onClick={() => client?.stopReply()}
          >
            <Square size={15} /> Interrupt
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
          <span role="status">{client?.state || "Connecting…"}</span>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (draft.trim()) {
              void send(draft);
              setDraft("");
            }
          }}
        >
          <input
            aria-label="Message voice agent"
            placeholder="Ask about tasks or describe one to create…"
            value={draft}
            maxLength={4000}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button
            className="primary-button"
            disabled={!client || !draft.trim() || !ready?.modelReady}
            aria-label="Send message"
          >
            <Send size={17} /> Send
          </button>
        </form>
        <p className="voice-footnote">
          Connected to Voice Lab · Conversation clears when you leave this page
          · Task status is checked when you ask
        </p>
      </footer>
    </section>
  );
}
