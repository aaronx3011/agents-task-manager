import { useState } from "react";
import { Pressable, ScrollView, View } from "react-native";
import type { Schemas, Task } from "@/api/client";
import { Avatar, Badge, Button, Choice, Label, Panel, Picker, Row, Sheet, Toggle } from "@/ui/kit";
import { age, overdue } from "@/ui/format";
import { STATUS_LABELS, usePalette } from "@/ui/theme";
import { EMPTY_FILTERS, ORDERINGS } from "@/tasks/filters";
import type { Filters, Ordering, Status } from "@/tasks/filters";

type Agent = Schemas["WorkspaceAgent"];
type Project = Schemas["WorkspaceProject"];

export const UNASSIGNED = "__unclaimed__";

export function TaskCard({ task, onPress }: { task: Task; onPress: () => void }) {
  const late = task.status === "in_progress" && overdue(task.heartbeat_at);
  return (
    <Panel accessibilityLabel={`Open task: ${task.title}`} onPress={onPress}>
      <Row style={{ justifyContent: "space-between" }}>
        <Label tone="muted" size={13}>
          #{task.id} · {task.project}
        </Label>
        <Badge status={task.status} />
      </Row>
      <Label size={16} bold numberOfLines={2}>
        {task.title}
      </Label>
      <Row gap={6}>
        <Label tone="secondary" size={13}>
          P{task.priority}
        </Label>
        <Label tone="secondary" size={13}>
          · {task.claimed_by ?? "Unassigned"}
        </Label>
        {task.dependency_progress.total ? (
          <Label tone={task.is_ready ? "secondary" : "danger"} size={13}>
            · deps {task.dependency_progress.done}/{task.dependency_progress.total}
          </Label>
        ) : null}
        {task.status === "pending" && task.is_ready ? (
          <Label tone="accent" size={13}>
            · ready
          </Label>
        ) : null}
        {late ? (
          <Label tone="danger" size={13}>
            · heartbeat overdue
          </Label>
        ) : null}
      </Row>
      <Label tone="muted" size={12}>
        Updated {age(task.updated_at)}
      </Label>
    </Panel>
  );
}

/** The agent's state, as the web's agent strip words it. */
export function agentLabel(agent: Agent, now = Date.now()) {
  const first = agent.assignments[0];
  const unseen = agent.last_seen ? now - Date.parse(agent.last_seen) : Infinity;
  const late =
    first?.status === "in_progress" &&
    (unseen > 900_000 || (!!first.heartbeat_at && now - Date.parse(first.heartbeat_at) > 900_000));
  let label = unseen > 300_000 ? "Away" : "Idle";
  if (first)
    label =
      first.status === "blocked"
        ? "Blocked"
        : first.status === "review"
          ? "In review"
          : late
            ? "Overdue"
            : unseen > 300_000
              ? "Quiet"
              : "Working";
  if (!agent.last_seen) label = first ? "Awaiting agent" : "Not seen";
  if (!agent.is_active) label = "Inactive";
  return {
    label,
    attention: ["Blocked", "Overdue", "Quiet", "Awaiting agent"].includes(label),
    first,
  };
}

export function AgentStrip({
  agents,
  selected,
  onFilter,
  onOpen,
}: {
  agents: Agent[];
  selected: string;
  onFilter: (name: string) => void;
  onOpen: (id: number) => void;
}) {
  const palette = usePalette();
  if (!agents.length) return null;
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={{ gap: 8 }}
    >
      {agents.map((agent) => {
        const { label, attention, first } = agentLabel(agent);
        const active = selected === agent.name;
        return (
          <View
            key={agent.name}
            style={{
              borderWidth: 1,
              borderRadius: 10,
              padding: 10,
              gap: 4,
              minWidth: 150,
              borderColor: active ? palette.accent : attention ? palette.danger : palette.line,
              backgroundColor: palette.panel,
            }}
          >
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={`Filter by ${agent.name}`}
              accessibilityState={{ selected: active }}
              onPress={() => onFilter(active ? "" : agent.name)}
              style={{ flexDirection: "row", alignItems: "center", gap: 6 }}
            >
              <Avatar name={agent.name} />
              <Label bold numberOfLines={1} style={{ maxWidth: 110 }}>
                {agent.name}
              </Label>
            </Pressable>
            {first ? (
              <Pressable
                accessibilityRole="button"
                accessibilityLabel={`Open task ${first.id}: ${first.title}`}
                onPress={() => onOpen(first.id)}
              >
                <Label size={12} tone={attention ? "danger" : "accent"}>
                  {label} · #{first.id}
                </Label>
                <Label size={12} tone="muted" numberOfLines={1} style={{ maxWidth: 150 }}>
                  {first.title}
                </Label>
              </Pressable>
            ) : (
              <Label size={12} tone={attention ? "danger" : "muted"}>
                {label}
              </Label>
            )}
            <Label size={11} tone="muted">
              {agent.last_seen ? `Seen ${age(agent.last_seen)}` : "Never seen"}
            </Label>
          </View>
        );
      })}
    </ScrollView>
  );
}

export function FilterSheet({
  visible,
  close,
  filters,
  ordering,
  showOrdering,
  projects,
  agents,
  apply,
}: {
  visible: boolean;
  close: () => void;
  filters: Filters;
  ordering: Ordering;
  showOrdering: boolean;
  projects: Project[];
  agents: Agent[];
  apply: (filters: Filters, ordering: Ordering) => void;
}) {
  const [draft, setDraft] = useState(filters);
  const [order, setOrder] = useState(ordering);
  const set = (changes: Partial<Filters>) => setDraft((current) => ({ ...current, ...changes }));
  return (
    <Sheet visible={visible} title="Filter tasks" onClose={close}>
      <Picker
        label="Project"
        value={draft.project}
        placeholder="All projects"
        options={[
          { value: "", label: "All projects" },
          ...projects.map((p) => ({
            value: p.slug,
            label: `${p.name} (${p.active_count} active)`,
          })),
        ]}
        onChange={(project) => set({ project })}
      />
      <Picker
        label="Agent"
        value={draft.claimed_by}
        placeholder="All agents"
        options={[
          { value: "", label: "All agents" },
          { value: UNASSIGNED, label: "Unassigned" },
          ...agents.map((a) => ({ value: a.name, label: a.name })),
        ]}
        onChange={(claimed_by) => set({ claimed_by })}
      />
      <Choice<Status | "">
        label="Status"
        value={draft.status}
        options={[
          { value: "", label: "All" },
          ...(Object.keys(STATUS_LABELS) as Status[]).map((status) => ({
            value: status,
            label: STATUS_LABELS[status],
          })),
        ]}
        onChange={(status) => set({ status })}
      />
      <Toggle
        label="Only tasks whose prerequisites are done (ready)"
        value={draft.ready === "true"}
        onChange={(ready) => set({ ready: ready ? "true" : "" })}
      />
      {showOrdering ? (
        <Picker label="Sort by" value={order} options={ORDERINGS} onChange={setOrder} />
      ) : null}
      <Row>
        <Button
          label="Apply"
          kind="primary"
          onPress={() => {
            apply(draft, order);
            close();
          }}
        />
        <Button
          label="Clear filters"
          onPress={() => {
            setDraft({ ...EMPTY_FILTERS, q: filters.q });
            setOrder("priority");
          }}
        />
      </Row>
    </Sheet>
  );
}
