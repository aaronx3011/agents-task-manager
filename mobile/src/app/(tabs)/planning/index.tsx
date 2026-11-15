// Project planning (#79): board, queue and settings per project, with
// play/pause/stop, matching the web Planning section.
import { useState } from "react";
import { Stack } from "expo-router";
import { useQuery } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type { PlanningCard } from "@/api/types";
import { useAccess } from "@/auth/access";
import { useSession } from "@/auth/session";
import { editableCard, usePlan, usePlanMutation } from "@/planning/api";
import {
  CardEditor,
  CardView,
  LaunchSheet,
  NewProjectSheet,
  QueueView,
  SettingsForm,
} from "@/planning/parts";
import { useWorkers } from "@/sessions/api";
import {
  Button,
  Choice,
  Empty,
  ErrorState,
  Heading,
  Label,
  Loading,
  Notice,
  Picker,
  Row,
  Screen,
  usePullRefresh,
} from "@/ui/kit";

type Tab = "board" | "queue" | "settings";

function ProjectPlanning({ slug, initialTab }: { slug: string; initialTab: Tab }) {
  const { canWrite } = useAccess();
  const [tab, setTab] = useState<Tab>(initialTab);
  const [column, setColumn] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ card: PlanningCard; revision: number } | null>(null);
  const [launch, setLaunch] = useState<PlanningCard | null>(null);
  const plan = usePlan(slug);
  const workers = useWorkers(0, 500);
  const data = plan.data;
  const { mutate, busy, error, clearError } = usePlanMutation(slug, data);
  if (!data)
    return plan.isError ? (
      <ErrorState error={plan.error} retry={() => void plan.refetch()} />
    ) : (
      <Loading label="Loading project board…" />
    );
  const queued = data.jobs.filter((job) => job.status === "queued");
  const active = data.jobs.find((job) => job.status === "running");
  const current =
    column && data.settings.columns.includes(column) ? column : data.settings.columns[0];
  const cards = data.cards.filter((card) => card.column === current);
  const move = (card: PlanningCard, target: string, position = card.position) =>
    void mutate({ operation: "card", card: { ...editableCard(card), column: target, position } });
  const edit = (card: PlanningCard) => {
    clearError();
    setEditor({ card: { ...card }, revision: data.revision });
  };
  return (
    <>
      <Row style={{ justifyContent: "space-between" }}>
        <Choice
          value={tab}
          options={[
            { value: "board", label: "Board" },
            { value: "queue", label: `Queue (${queued.length})` },
            { value: "settings", label: "Settings" },
          ]}
          onChange={setTab}
        />
      </Row>
      <Label tone="secondary" size={13}>
        {data.playing
          ? active
            ? `Running: ${active.title}`
            : "Waiting for worker sync"
          : data.pause_reason || "Queue paused"}
      </Label>
      {canWrite ? (
        <Row>
          <Button
            small
            kind="primary"
            label={data.playing ? "❚❚ Pause" : "▶ Play"}
            accessibilityLabel={data.playing ? "Pause" : "Play"}
            disabled={busy || (!active && !queued.length)}
            onPress={() => void mutate({ operation: data.playing ? "pause" : "play" })}
          />
          {active ? (
            <Button
              small
              kind="danger"
              label="■ Stop current"
              accessibilityLabel="Stop current"
              disabled={busy}
              onPress={() => void mutate({ operation: "stop" })}
            />
          ) : null}
        </Row>
      ) : (
        <Notice>
          Viewers can follow planning but not change it. Ask an owner for member access.
        </Notice>
      )}
      {error && !editor && !launch ? <Notice error>{error}</Notice> : null}
      {plan.isError ? <Notice error>Updates interrupted: {plan.error.message}</Notice> : null}
      {tab === "settings" ? (
        <SettingsForm
          key={data.revision}
          initial={data.settings}
          revision={data.revision}
          workers={workers.data?.items ?? []}
          busy={busy}
          error={error}
          canWrite={canWrite}
          save={(settings, revision) => mutate({ operation: "settings", settings }, revision)}
        />
      ) : tab === "queue" ? (
        <QueueView
          plan={data}
          mutate={mutate}
          busy={busy}
          canWrite={canWrite}
          refresh={() => void plan.refetch()}
        />
      ) : (
        <>
          <Choice
            value={current ?? ""}
            options={data.settings.columns.map((name) => ({
              value: name,
              label: `${name} ${data.cards.filter((card) => card.column === name).length}`,
            }))}
            onChange={setColumn}
          />
          {canWrite && current ? (
            <Button
              small
              label={`+ Add card to ${current}`}
              onPress={() =>
                edit({
                  title: "",
                  description: "",
                  column: current,
                  labels: [],
                  checklist: [],
                  instructions: "",
                })
              }
            />
          ) : null}
          {cards.length ? (
            cards.map((card) => (
              <CardView
                key={card.id}
                card={card}
                plan={data}
                busy={busy}
                canWrite={canWrite}
                edit={() => (canWrite ? edit(card) : undefined)}
                launch={() => {
                  clearError();
                  setLaunch(card);
                }}
                move={(target) =>
                  move(
                    card,
                    target,
                    Math.max(
                      0,
                      ...data.cards.filter((c) => c.column === target).map((c) => c.position || 0),
                    ) + 1,
                  )
                }
              />
            ))
          ) : (
            <Empty title="No cards in this column" />
          )}
          <Label tone="muted" size={12}>
            Use a card’s Move menu to change its column. Board columns are independent of task
            execution status.
          </Label>
        </>
      )}
      {editor ? (
        <CardEditor
          initial={editor.card}
          revision={editor.revision}
          mutate={mutate}
          busy={busy}
          error={error}
          close={() => setEditor(null)}
        />
      ) : null}
      {launch ? (
        <LaunchSheet
          card={launch}
          plan={data}
          mutate={mutate}
          busy={busy}
          error={error}
          close={() => setLaunch(null)}
          launched={() => {
            setLaunch(null);
            setTab("queue");
          }}
        />
      ) : null}
    </>
  );
}

export default function Planning() {
  const { api } = useSession();
  const { canWrite } = useAccess();
  const [chosen, setChosen] = useState("");
  const [creating, setCreating] = useState(false);
  const [openSettings, setOpenSettings] = useState<string | null>(null);
  const workspace = useQuery({
    queryKey: ["workspace", "projects"],
    enabled: !!api,
    queryFn: () => unwrap(api!.GET("/api/v1/dashboard/workspace", { params: { query: {} } })),
  });
  const projects = workspace.data?.projects ?? [];
  const slug = chosen || projects[0]?.slug || "";
  const pull = usePullRefresh(() => workspace.refetch());
  return (
    <Screen refreshing={pull.refreshing} onRefresh={pull.onRefresh}>
      <Stack.Screen options={{ title: "Planning" }} />
      <Heading eyebrow="Plan → execute → review">Project planning</Heading>
      <Label tone="secondary" size={13}>
        Organize your work. Launch contextual sessions in order.
      </Label>
      {workspace.error ? (
        <ErrorState error={workspace.error} retry={() => void workspace.refetch()} />
      ) : null}
      {projects.length ? (
        <Picker
          label="Project"
          value={slug}
          options={projects.map((project) => ({ value: project.slug, label: project.name }))}
          onChange={(value) => {
            setChosen(value);
            setOpenSettings(null);
          }}
        />
      ) : null}
      {canWrite ? <Button label="+ New project" onPress={() => setCreating(true)} /> : null}
      {workspace.isLoading ? (
        <Loading label="Loading projects" />
      ) : slug ? (
        <ProjectPlanning
          key={slug}
          slug={slug}
          initialTab={openSettings === slug ? "settings" : "board"}
        />
      ) : (
        <Empty
          title="No projects yet"
          detail="Create your first project with New project to start planning."
        />
      )}
      {creating && canWrite ? (
        <NewProjectSheet
          close={() => setCreating(false)}
          created={(created) => {
            setCreating(false);
            setChosen(created);
            setOpenSettings(created);
          }}
        />
      ) : null}
    </Screen>
  );
}
