import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { MessagesSquare, Mic, MicOff, Play, Send, Trash2 } from "lucide-react";
import { ApiError } from "./api-client";
import {
  PAGE,
  addParticipant,
  advanceRoom,
  closeRoom,
  createRoom,
  listModels,
  listRooms,
  patchParticipant,
  readHistory,
  readRoom,
  removeParticipant,
  sendMessage,
} from "./chat-rooms";
import type { Message, Participant, Room } from "./chat-rooms";
import { Age, Empty, ErrorState, Loading, tone } from "./ui";
import "./chat-rooms.css";

function errorText(error: unknown) {
  return error instanceof ApiError || error instanceof Error
    ? error.message
    : "Something went wrong";
}

function Bubble({ message }: { message: Message }) {
  const label =
    message.authorKind === "participant"
      ? message.role
      : message.authorKind === "system"
        ? "System"
        : message.role || "You";
  return (
    <article className={`cr-bubble cr-${message.authorKind}`}>
      <header>
        <span className={`cr-speaker ${tone(label)}`}>{label}</span>
        {message.model && (
          <span className="cr-model" title={`${message.model.providerID}`}>
            {message.model.modelID.split("/").pop()}
          </span>
        )}
        <Age value={message.createdAt} />
      </header>
      <p>{message.text}</p>
      {message.toolEvents.length > 0 && (
        <footer className="cr-tools">
          {message.toolEvents.map((event, index) => (
            <span
              className={`cr-tool ${event.is_error ? "cr-tool-error" : ""}`}
              key={`${event.tool}-${index}`}
            >
              {event.tool}
              {event.is_error ? " failed" : ""}
            </span>
          ))}
        </footer>
      )}
    </article>
  );
}

function ParticipantRail({
  room,
  models,
  canManage,
  onChanged,
}: {
  room: Room;
  models: string[];
  canManage: boolean;
  onChanged: () => void;
}) {
  const [role, setRole] = useState("");
  const [persona, setPersona] = useState("");
  const [model, setModel] = useState(models[0] ?? "");
  const [canAct, setCanAct] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const participants = room.participants ?? [];
  const speaking = room.pendingSpeakers[0];

  useEffect(() => {
    if (!model && models.length) setModel(models[0]);
  }, [model, models]);

  async function act(run: () => Promise<unknown>) {
    setBusy(true);
    setError("");
    try {
      await run();
      onChanged();
    } catch (problem) {
      setError(errorText(problem));
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="cr-rail" aria-label="Room participants">
      <h2>Participants</h2>
      {participants.length === 0 && (
        <p className="muted">
          Add at least one participant before starting a turn.
        </p>
      )}
      <ul>
        {participants.map((participant: Participant) => (
          <li
            className={`cr-participant ${participant.id === speaking ? "cr-speaking" : ""}`}
            key={participant.id}
          >
            <div>
              <strong className={tone(participant.role)}>
                {participant.role}
              </strong>
              <span
                className={`cr-badge ${participant.canAct ? "cr-acts" : ""}`}
              >
                {participant.canAct ? "acts" : "reads only"}
              </span>
              {participant.kind === "worker" && (
                <span className="cr-badge">worker</span>
              )}
            </div>
            <small>{participant.model?.modelID.split("/").pop()}</small>
            {participant.persona && (
              <small className="muted">
                {participant.persona.slice(0, 90)}
              </small>
            )}
            <div className="cr-participant-actions" hidden={!canManage}>
              <button
                className="oc-text-button"
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    patchParticipant(room.id, participant.id, {
                      isMuted: !participant.isMuted,
                    }),
                  )
                }
                title={participant.isMuted ? "Unmute" : "Mute"}
              >
                {participant.isMuted ? <MicOff size={13} /> : <Mic size={13} />}
                {participant.isMuted ? "Muted" : "Speaking"}
              </button>
              {!participant.canAct && (
                <button
                  className="oc-text-button"
                  disabled={busy}
                  onClick={() =>
                    void act(() =>
                      patchParticipant(room.id, participant.id, {
                        canAct: true,
                      }),
                    )
                  }
                  title="Only one participant per room may change anything"
                >
                  Let this one act
                </button>
              )}
              <button
                className="oc-text-button"
                disabled={busy}
                onClick={() =>
                  void act(() => removeParticipant(room.id, participant.id))
                }
              >
                <Trash2 size={13} /> Remove
              </button>
            </div>
          </li>
        ))}
      </ul>
      {room.status === "open" && canManage && (
        <form
          className="cr-add-participant"
          onSubmit={(event) => {
            event.preventDefault();
            void act(async () => {
              await addParticipant(room.id, {
                role,
                persona,
                model: { providerID: "fireworks", modelID: model },
                canAct,
              });
              setRole("");
              setPersona("");
              setCanAct(false);
            });
          }}
        >
          <h3>Add a role</h3>
          <label>
            Role
            <input
              maxLength={60}
              onChange={(event) => setRole(event.target.value)}
              placeholder="Architect"
              required
              value={role}
            />
          </label>
          <label>
            Character
            <textarea
              onChange={(event) => setPersona(event.target.value)}
              placeholder="You weigh long-term maintainability over speed."
              rows={3}
              value={persona}
            />
          </label>
          <label>
            Model
            <select
              onChange={(event) => setModel(event.target.value)}
              value={model}
            >
              {models.map((name) => (
                <option key={name} value={name}>
                  {name.split("/").pop()}
                </option>
              ))}
            </select>
          </label>
          <label className="cr-check">
            <input
              checked={canAct}
              onChange={(event) => setCanAct(event.target.checked)}
              type="checkbox"
            />
            May act (creates and changes things; at most one per room)
          </label>
          <button className="button" disabled={busy || !role || !model}>
            Add participant
          </button>
        </form>
      )}
      {error && (
        <p className="cr-error" role="alert">
          {error}
        </p>
      )}
    </aside>
  );
}

export default function ChatRooms({
  project,
  canManage,
}: {
  project: string;
  canManage: boolean;
}) {
  const client = useQueryClient();
  const [selected, setSelected] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const [newRoom, setNewRoom] = useState("");
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const stop = useRef(false);

  const rooms = useQuery({
    queryKey: ["chatrooms", "list", project],
    queryFn: () => listRooms(project || undefined),
  });
  const models = useQuery({
    queryKey: ["chatrooms", "models"],
    queryFn: listModels,
  });
  const room = useQuery({
    queryKey: ["chatrooms", "room", selected],
    queryFn: () => readRoom(selected as number),
    enabled: selected !== null,
  });
  const history = useQuery({
    queryKey: ["chatrooms", "history", selected],
    queryFn: () => readHistory(selected as number),
    enabled: selected !== null,
  });

  const modelNames = useMemo(
    () => (models.data?.models ?? []).map((item) => item.modelID),
    [models.data],
  );

  const refresh = useCallback(() => {
    void client.invalidateQueries({ queryKey: ["chatrooms"] });
  }, [client]);

  // Drive the turn: one request per speaker, so a long room never blocks on a
  // single long-lived HTTP request and each reply renders as it lands.
  const runTurn = useCallback(
    async (id: number) => {
      setRunning(true);
      stop.current = false;
      try {
        for (let guard = 0; guard < 24 && !stop.current; guard += 1) {
          const result = await advanceRoom(id);
          refresh();
          if (!result.advanced || result.room.pendingSpeakers.length === 0)
            break;
        }
      } catch (problem) {
        setError(errorText(problem));
      } finally {
        setRunning(false);
        refresh();
      }
    },
    [refresh],
  );

  async function submit() {
    if (selected === null || !draft.trim()) return;
    const text = draft;
    setDraft("");
    setError("");
    try {
      await sendMessage(selected, text);
      refresh();
      await runTurn(selected);
    } catch (problem) {
      setError(errorText(problem));
      setDraft(text);
    }
  }

  const current = room.data;
  const pending = current?.pendingSpeakers ?? [];
  const nextSpeaker = current?.participants?.find(
    (item) => item.id === pending[0],
  );

  return (
    <section className="cr-workspace" aria-label="Chat rooms">
      <aside className="cr-list" aria-label="Rooms">
        <h2>
          <MessagesSquare size={18} /> Rooms
        </h2>
        {rooms.isLoading ? (
          <Loading label="Loading rooms" />
        ) : rooms.error ? (
          <ErrorState error={rooms.error} retry={() => void rooms.refetch()} />
        ) : (
          <ul>
            {(rooms.data?.items ?? []).map((item) => (
              <li key={item.id}>
                <button
                  aria-pressed={selected === item.id}
                  className={`cr-room ${selected === item.id ? "selected" : ""}`}
                  onClick={() => setSelected(item.id)}
                >
                  <strong>{item.name}</strong>
                  <small>
                    {item.project} · {item.messageCount} messages
                    {item.status === "closed" ? " · closed" : ""}
                  </small>
                </button>
              </li>
            ))}
          </ul>
        )}
        {!rooms.data?.count && !rooms.isLoading && (
          <Empty
            title="No rooms yet"
            description="Create one to get started."
          />
        )}
        {canManage && (
          <form
            className="cr-new-room"
            onSubmit={(event) => {
              event.preventDefault();
              if (!project) {
                setError("Choose a project in the sidebar first");
                return;
              }
              void (async () => {
                setError("");
                try {
                  const created = await createRoom({
                    project,
                    name: newRoom,
                  });
                  setNewRoom("");
                  setSelected(created.id);
                  refresh();
                } catch (problem) {
                  setError(errorText(problem));
                }
              })();
            }}
          >
            <label>
              New room
              <input
                maxLength={120}
                onChange={(event) => setNewRoom(event.target.value)}
                placeholder="Design review"
                required
                value={newRoom}
              />
            </label>
            <button className="button secondary" disabled={!newRoom}>
              Create room
            </button>
          </form>
        )}
      </aside>

      {current ? (
        <section className="cr-room-view" aria-label={`Room ${current.name}`}>
          <header className="cr-heading">
            <div>
              <div className="eyebrow">MULTI-AGENT ROOM</div>
              <h1>{current.name}</h1>
              <p>
                {current.topic || "No topic set."} · turn {current.turn} ·{" "}
                {current.messageCount} messages
              </p>
            </div>
            {current.status === "open" && canManage && (
              <button
                className="button secondary"
                onClick={() =>
                  void (async () => {
                    await closeRoom(current.id);
                    refresh();
                  })()
                }
              >
                Close room
              </button>
            )}
          </header>

          <div className="cr-transcript" aria-live="polite">
            {history.isLoading ? (
              <Loading label="Loading transcript" />
            ) : history.error ? (
              <ErrorState
                error={history.error}
                retry={() => void history.refetch()}
              />
            ) : (
              <>
                {history.data?.hasMore && (
                  <button
                    className="oc-text-button"
                    onClick={() =>
                      void (async () => {
                        const older = await readHistory(
                          current.id,
                          history.data?.olderCursor ?? undefined,
                        );
                        client.setQueryData(
                          ["chatrooms", "history", current.id],
                          {
                            ...history.data,
                            messages: [
                              ...older.messages,
                              ...(history.data?.messages ?? []),
                            ],
                            olderCursor: older.olderCursor,
                            hasMore: older.hasMore,
                          },
                        );
                      })()
                    }
                  >
                    Load {PAGE} earlier messages
                  </button>
                )}
                {(history.data?.messages ?? []).map((message) => (
                  <Bubble key={message.id} message={message} />
                ))}
                {!history.data?.messages.length && (
                  <Empty title="Nothing said yet" />
                )}
              </>
            )}
          </div>

          <div className="cr-turn-state">
            {pending.length > 0 ? (
              <span className="cr-thinking">
                {nextSpeaker
                  ? `${nextSpeaker.role} is thinking…`
                  : "Turn in progress…"}{" "}
                · {pending.length} to speak
              </span>
            ) : (
              <span className="muted">Nobody is queued to speak.</span>
            )}
            {running ? (
              <button
                className="button secondary"
                onClick={() => {
                  stop.current = true;
                }}
              >
                Stop after this speaker
              </button>
            ) : (
              pending.length > 0 &&
              canManage && (
                <button
                  className="button secondary"
                  onClick={() => void runTurn(current.id)}
                >
                  <Play size={14} /> Continue the turn
                </button>
              )
            )}
            {!running &&
              pending.length === 0 &&
              current.status === "open" &&
              canManage && (
                <button
                  className="button secondary"
                  onClick={() =>
                    void (async () => {
                      setError("");
                      try {
                        await advanceRoom(current.id, { rounds: 1 });
                        refresh();
                        await runTurn(current.id);
                      } catch (problem) {
                        setError(errorText(problem));
                      }
                    })()
                  }
                >
                  <Play size={14} /> Another round
                </button>
              )}
          </div>

          {current.status === "open" && canManage && (
            <form
              className="cr-composer"
              onSubmit={(event) => {
                event.preventDefault();
                void submit();
              }}
            >
              <textarea
                maxLength={20000}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="Ask the room something…"
                rows={2}
                value={draft}
              />
              <button className="button" disabled={running || !draft.trim()}>
                <Send size={15} /> Send
              </button>
            </form>
          )}
          {error && (
            <p className="cr-error" role="alert">
              {error}
            </p>
          )}
        </section>
      ) : (
        <section className="cr-room-view">
          <Empty
            title="Select a room"
            description="Pick a room on the left, or create one."
          />
        </section>
      )}

      {current && (
        <ParticipantRail
          canManage={canManage}
          models={modelNames}
          onChanged={refresh}
          room={current}
        />
      )}
    </section>
  );
}
