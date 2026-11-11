import { useState } from "react";
import { Pressable, View } from "react-native";
import { router } from "expo-router";
import { useQueryClient } from "@tanstack/react-query";
import { unwrap } from "@/api/client";
import type {
  ModelChoice,
  Plan,
  PlanningCard,
  PlanningJob,
  PlanningSettings,
  Worker,
} from "@/api/types";
import { useSession } from "@/auth/session";
import { slugify } from "@/planning/api";
import { openSession } from "@/sessions/nav";
import {
  Badge,
  Button,
  Choice,
  Disclosure,
  Field,
  Label,
  Notice,
  Panel,
  Picker,
  Row,
  Sheet,
  Toggle,
} from "@/ui/kit";
import { errorText } from "@/ui/format";
import { Markdown } from "@/ui/markdown";

type Mutate = (values: Record<string, unknown>, revision?: number) => Promise<boolean>;

const KINDS = [
  { value: "code", label: "Code" },
  { value: "design", label: "Design" },
  { value: "content", label: "Content" },
  { value: "other", label: "Other" },
] as const;

export function NewProjectSheet({
  close,
  created,
}: {
  close: () => void;
  created: (slug: string) => void;
}) {
  const { api } = useSession();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugEdited, setSlugEdited] = useState(false);
  const [kind, setKind] = useState<(typeof KINDS)[number]["value"]>("code");
  const [repo, setRepo] = useState("");
  const [docs, setDocs] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Sheet visible title="New project" onClose={() => !busy && close()}>
      <Label tone="secondary" size={13}>
        Create a project, then configure its worker, directory, model and prompts.
      </Label>
      <Field
        label="Project name"
        maxLength={200}
        value={name}
        onChangeText={(value) => {
          setName(value);
          if (!slugEdited) setSlug(slugify(value));
        }}
      />
      <Field
        label="Project slug"
        maxLength={50}
        autoCapitalize="none"
        autoCorrect={false}
        value={slug}
        hint="Unique identifier for links and agent tools. Letters, numbers, hyphens and underscores."
        onChangeText={(value) => {
          setSlug(value);
          setSlugEdited(true);
        }}
      />
      <Choice label="Project kind" value={kind} options={[...KINDS]} onChange={setKind} />
      <Field
        label="Repository URL (optional)"
        maxLength={200}
        autoCapitalize="none"
        keyboardType="url"
        value={repo}
        onChangeText={setRepo}
        placeholder="https://github.com/you/project"
      />
      <Field
        label="Documentation reference (optional)"
        maxLength={500}
        autoCapitalize="none"
        value={docs}
        onChangeText={setDocs}
        placeholder="Documentation URL or path"
      />
      {error ? <Notice error>{error}</Notice> : null}
      <Row>
        <Button
          kind="primary"
          label={busy ? "Creating…" : "Create project"}
          disabled={busy || !name.trim() || !/^[a-zA-Z0-9_-]+$/.test(slug)}
          onPress={async () => {
            if (!api) return;
            setBusy(true);
            setError("");
            try {
              const project = await unwrap(
                api.POST("/api/v1/projects", {
                  body: { name, slug, kind, repo_url: repo, docs_path: docs } as never,
                }),
              );
              await client.invalidateQueries({ queryKey: ["workspace"] });
              created((project as { slug: string }).slug);
            } catch (e) {
              setError(errorText(e, "Project creation failed"));
            } finally {
              setBusy(false);
            }
          }}
        />
        <Button label="Cancel" disabled={busy} onPress={close} />
      </Row>
    </Sheet>
  );
}

export function CardView({
  card,
  plan,
  busy,
  edit,
  launch,
  move,
  canWrite,
}: {
  card: PlanningCard;
  plan: Plan;
  busy: boolean;
  edit: () => void;
  launch: () => void;
  move: (column: string) => void;
  canWrite: boolean;
}) {
  const running = plan.jobs.some(
    (job) => job.card === card.id && ["queued", "running"].includes(job.status),
  );
  return (
    <Panel>
      {card.labels.length ? (
        <Row gap={6}>
          {card.labels.map((label) => (
            <Badge key={label} status="label" label={label} />
          ))}
        </Row>
      ) : null}
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Edit card ${card.title}`}
        disabled={!canWrite}
        onPress={edit}
      >
        <Label size={16} bold tone={canWrite ? "accent" : "text"}>
          {card.title}
        </Label>
      </Pressable>
      {card.description ? (
        <Label tone="secondary" size={13} numberOfLines={4}>
          {card.description.slice(0, 160)}
        </Label>
      ) : null}
      {card.checklist.length ? (
        <Label tone="muted" size={12}>
          Checklist {card.checklist.filter((item) => item.done).length}/{card.checklist.length}
        </Label>
      ) : null}
      {card.task ? (
        <Button
          small
          label={`Task #${card.task} · ${card.task_status ?? ""}`}
          onPress={() => router.push({ pathname: "/tasks/[id]", params: { id: card.task! } })}
        />
      ) : null}
      {canWrite ? (
        <Row>
          <Button
            small
            kind="primary"
            label="Run / Queue"
            disabled={busy || running}
            onPress={launch}
          />
          <Picker
            label="Move to"
            value={card.column}
            options={plan.settings.columns.map((column) => ({ value: column, label: column }))}
            onChange={(column) => column !== card.column && move(column)}
            disabled={busy}
          />
        </Row>
      ) : null}
    </Panel>
  );
}

export function CardEditor({
  initial,
  revision,
  mutate,
  busy,
  error,
  close,
}: {
  initial: PlanningCard;
  revision: number;
  mutate: Mutate;
  busy: boolean;
  error: string;
  close: () => void;
}) {
  const [card, setCard] = useState(initial);
  const [labels, setLabels] = useState(initial.labels.join(", "));
  const save = async () => {
    const ok = await mutate(
      {
        operation: "card",
        card: {
          ...card,
          labels: [
            ...new Set(
              labels
                .split(",")
                .map((label) => label.trim())
                .filter(Boolean),
            ),
          ],
        },
      },
      revision,
    );
    if (ok) close();
  };
  return (
    <Sheet visible title={card.id ? "Edit card" : "New card"} onClose={close}>
      <Field
        label="Title"
        maxLength={300}
        value={card.title}
        onChangeText={(title) => setCard({ ...card, title })}
      />
      <Field
        label="Description / acceptance criteria"
        multiline
        maxLength={12000}
        value={card.description}
        onChangeText={(description) => setCard({ ...card, description })}
      />
      <Field
        label="Labels (comma separated)"
        value={labels}
        onChangeText={setLabels}
        autoCapitalize="none"
      />
      <Label tone="secondary" size={13}>
        Checklist
      </Label>
      {card.checklist.map((item, index) => (
        <View key={index} style={{ gap: 6 }}>
          <Toggle
            label={item.text || `Checklist item ${index + 1}`}
            value={item.done}
            onChange={(done) =>
              setCard({
                ...card,
                checklist: card.checklist.map((c, n) => (n === index ? { ...c, done } : c)),
              })
            }
          />
          <Row wrap={false}>
            <View style={{ flex: 1 }}>
              <Field
                label={`Checklist item ${index + 1}`}
                maxLength={500}
                value={item.text}
                onChangeText={(text) =>
                  setCard({
                    ...card,
                    checklist: card.checklist.map((c, n) => (n === index ? { ...c, text } : c)),
                  })
                }
              />
            </View>
            <Button
              small
              kind="danger"
              label="Remove"
              accessibilityLabel={`Remove checklist item ${index + 1}`}
              onPress={() =>
                setCard({ ...card, checklist: card.checklist.filter((_, n) => n !== index) })
              }
            />
          </Row>
        </View>
      ))}
      <Button
        small
        label="Add checklist item"
        onPress={() =>
          setCard({ ...card, checklist: [...card.checklist, { text: "", done: false }] })
        }
      />
      <Field
        label="Default instructions for this card"
        multiline
        maxLength={8000}
        value={card.instructions}
        onChangeText={(instructions) => setCard({ ...card, instructions })}
      />
      {error ? <Notice error>{error}</Notice> : null}
      <Row>
        <Button
          kind="primary"
          label="Save card"
          disabled={busy || !card.title.trim() || card.checklist.some((item) => !item.text.trim())}
          onPress={() => void save()}
        />
        {card.id ? (
          <Button
            kind="danger"
            label="Archive"
            disabled={busy}
            onPress={async () => {
              if (await mutate({ operation: "archive", card_id: card.id }, revision)) close();
            }}
          />
        ) : null}
        <Button label="Cancel" onPress={close} />
      </Row>
    </Sheet>
  );
}

export function LaunchSheet({
  card,
  plan,
  mutate,
  busy,
  error,
  close,
  launched,
}: {
  card: PlanningCard;
  plan: Plan;
  mutate: Mutate;
  busy: boolean;
  error: string;
  close: () => void;
  launched: () => void;
}) {
  const [template, setTemplate] = useState("");
  const [instructions, setInstructions] = useState(card.instructions);
  const queued = plan.jobs.filter((job) => job.status === "queued");
  const active = plan.jobs.find((job) => job.status === "running");
  const { settings } = plan;
  return (
    <Sheet visible title={`Run: ${card.title}`} onClose={close}>
      <Label tone="secondary" size={13}>
        {settings.worker || "No worker configured"} ·{" "}
        {settings.directory || "Worker default directory"} ·{" "}
        {settings.model?.modelID || "Worker default model"}
      </Label>
      <Picker
        label="Prompt template"
        value={template}
        options={[
          { value: "", label: "None" },
          ...settings.templates.map((t) => ({ value: t.text, label: t.name })),
        ]}
        onChange={setTemplate}
      />
      <Field
        label="What should the agent do?"
        multiline
        maxLength={8000}
        value={instructions}
        onChangeText={setInstructions}
      />
      <Disclosure title="Preview context">
        <Markdown
          size={12}
          text={
            "```\n" +
            JSON.stringify(
              {
                project: plan.project,
                context: settings.context,
                standingInstructions: settings.instructions,
                card,
                template,
                instructions,
              },
              null,
              2,
            ) +
            "\n```"
          }
        />
      </Disclosure>
      <Label tone="muted" size={12}>
        Settings are captured when you add this task. Previous completed queue results are added
        when its session starts.
      </Label>
      {!settings.worker ? (
        <Notice>Choose a default worker in Settings before running cards.</Notice>
      ) : null}
      {error ? <Notice error>{error}</Notice> : null}
      <Row>
        {(["enqueue", "run"] as const).map((operation) => (
          <Button
            key={operation}
            kind={operation === "run" ? "primary" : "secondary"}
            label={operation === "run" ? "Start session" : "Add to queue"}
            disabled={
              busy || !settings.worker || (operation === "run" && (!!active || !!queued.length))
            }
            onPress={async () => {
              if (await mutate({ operation, card_id: card.id, instructions, template })) launched();
            }}
          />
        ))}
      </Row>
    </Sheet>
  );
}

function CompletionForm({ task, done }: { task: number; done: () => void }) {
  const { api } = useSession();
  const [summary, setSummary] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <View style={{ gap: 6 }}>
      <Field
        label="Explicit completion / handoff"
        multiline
        value={summary}
        onChangeText={setSummary}
        placeholder="Verified outcome and context for the next task"
      />
      <Button
        small
        kind="primary"
        label="Mark task completed"
        disabled={busy || !summary.trim()}
        onPress={async () => {
          if (!api) return;
          setBusy(true);
          setError("");
          try {
            await unwrap(
              api.POST("/api/v1/tasks/{id}/status", {
                params: { path: { id: String(task) } },
                body: { status: "done", result_summary: summary } as never,
              }),
            );
            setSummary("");
            done();
          } catch (e) {
            setError(errorText(e, "Completion failed"));
          } finally {
            setBusy(false);
          }
        }}
      />
      {error ? <Notice error>{error}</Notice> : null}
    </View>
  );
}

export function QueueView({
  plan,
  mutate,
  busy,
  canWrite,
  refresh,
}: {
  plan: Plan;
  mutate: Mutate;
  busy: boolean;
  canWrite: boolean;
  refresh: () => void;
}) {
  const queued = plan.jobs.filter((job) => job.status === "queued");
  const reorder = (job: PlanningJob, direction: number) => {
    const ids = queued.map((j) => j.id);
    const index = ids.indexOf(job.id);
    [ids[index], ids[index + direction]] = [ids[index + direction], ids[index]];
    void mutate({ operation: "reorder", job_ids: ids });
  };
  return (
    <View style={{ gap: 10 }}>
      <Label tone="secondary" size={13}>
        One task at a time. Each gets a fresh session and frozen project/card context. Explicit task
        completion advances the queue; idle sessions do not.
      </Label>
      {!plan.jobs.length ? (
        <Label tone="muted">
          Add cards to the queue from the board, arrange them, then press Play.
        </Label>
      ) : null}
      {plan.jobs.map((job) => {
        const position = queued.indexOf(job);
        return (
          <Panel key={job.id}>
            <Row style={{ justifyContent: "space-between" }}>
              <Label bold style={{ flex: 1 }}>
                {job.title}
              </Label>
              <Badge
                status={job.status === "running" ? "in_progress" : job.status}
                label={job.status}
              />
            </Row>
            <Label tone="muted" size={12}>
              {job.worker}
              {job.task_status ? ` · task ${job.task_status}` : ""}
            </Label>
            {job.detail ? (
              <Label tone="secondary" size={13}>
                {job.detail}
              </Label>
            ) : null}
            <Row>
              {job.status === "queued" && canWrite ? (
                <>
                  <Button
                    small
                    label="↑"
                    accessibilityLabel={`Move ${job.title} up`}
                    disabled={busy || position <= 0}
                    onPress={() => reorder(job, -1)}
                  />
                  <Button
                    small
                    label="↓"
                    accessibilityLabel={`Move ${job.title} down`}
                    disabled={busy || position < 0 || position >= queued.length - 1}
                    onPress={() => reorder(job, 1)}
                  />
                  <Button
                    small
                    kind="danger"
                    label="Remove"
                    disabled={busy}
                    onPress={() => void mutate({ operation: "remove", job_id: job.id })}
                  />
                </>
              ) : null}
              {job.session ? (
                <Button
                  small
                  label="Open session"
                  onPress={() => openSession({ worker: job.worker, sessionID: job.session })}
                />
              ) : null}
              {job.task ? (
                <Button
                  small
                  label={`Task #${job.task}`}
                  onPress={() =>
                    router.push({ pathname: "/tasks/[id]", params: { id: job.task! } })
                  }
                />
              ) : null}
            </Row>
            <Disclosure title="Context and first prompt">
              <Markdown
                size={12}
                text={"```\n" + (job.prompt || JSON.stringify(job.snapshot, null, 2)) + "\n```"}
              />
            </Disclosure>
            {job.task && ["running", "failed"].includes(job.status) && canWrite ? (
              <CompletionForm task={job.task} done={refresh} />
            ) : null}
          </Panel>
        );
      })}
    </View>
  );
}

export function SettingsForm({
  initial,
  revision,
  workers,
  busy,
  error,
  canWrite,
  save,
}: {
  initial: PlanningSettings;
  revision: number;
  workers: Worker[];
  busy: boolean;
  error: string;
  canWrite: boolean;
  save: (settings: PlanningSettings, revision: number) => Promise<boolean>;
}) {
  const [draft, setDraft] = useState(initial);
  const [base, setBase] = useState(revision);
  const [saved, setSaved] = useState(false);
  const [columns, setColumns] = useState(initial.columns.join("\n"));
  const worker = workers.find((w) => w.name === draft.worker);
  const modelValue = draft.model
    ? JSON.stringify({ providerID: draft.model.providerID, modelID: draft.model.modelID })
    : "";
  return (
    <View style={{ gap: 12 }}>
      <Label size={17} bold>
        Project configuration
      </Label>
      <Label tone="secondary" size={13}>
        Defaults are inherited by new launches. Existing queued jobs retain their captured settings.
      </Label>
      <Picker
        label="Default worker"
        value={draft.worker}
        disabled={!canWrite}
        placeholder="Choose a worker"
        options={[
          { value: "", label: "Choose a worker" },
          ...workers.map((w) => ({
            value: w.name,
            label: `${w.name} · ${w.online ? "online" : "offline"}`,
          })),
        ]}
        onChange={(name) => {
          const w = workers.find((item) => item.name === name);
          setDraft({
            ...draft,
            worker: name,
            directory: w?.create_directory || w?.directory || "",
            model: null,
          });
        }}
      />
      <Field
        label="Default directory"
        maxLength={500}
        autoCapitalize="none"
        autoCorrect={false}
        editable={canWrite}
        placeholder="Absolute path on the selected worker"
        value={draft.directory}
        onChangeText={(directory) => setDraft({ ...draft, directory })}
      />
      <Picker
        label="Default model"
        value={modelValue}
        disabled={!canWrite}
        options={[
          { value: "", label: "Worker default" },
          ...(worker?.models ?? []).map((m) => ({
            value: JSON.stringify({ providerID: m.providerID, modelID: m.modelID }),
            label: `${m.providerID}/${m.modelID}`,
          })),
        ]}
        onChange={(value) =>
          setDraft({ ...draft, model: value ? (JSON.parse(value) as ModelChoice) : null })
        }
      />
      <Field
        label="Project context"
        multiline
        maxLength={8000}
        editable={canWrite}
        value={draft.context}
        onChangeText={(context) => setDraft({ ...draft, context })}
      />
      <Field
        label="Standing instructions — included in every session"
        multiline
        maxLength={8000}
        editable={canWrite}
        value={draft.instructions}
        onChangeText={(instructions) => setDraft({ ...draft, instructions })}
      />
      <Field
        label="Board columns — one per line"
        multiline
        editable={canWrite}
        value={columns}
        onChangeText={setColumns}
      />
      <Label size={15} bold>
        Reusable prompts
      </Label>
      {draft.templates.map((t, i) => (
        <Panel key={i}>
          <Field
            label="Prompt name"
            maxLength={100}
            editable={canWrite}
            value={t.name}
            onChangeText={(name) =>
              setDraft({
                ...draft,
                templates: draft.templates.map((p, n) => (i === n ? { ...p, name } : p)),
              })
            }
          />
          <Field
            label="Prompt text"
            multiline
            maxLength={8000}
            editable={canWrite}
            value={t.text}
            onChangeText={(text) =>
              setDraft({
                ...draft,
                templates: draft.templates.map((p, n) => (i === n ? { ...p, text } : p)),
              })
            }
          />
          {canWrite ? (
            <Button
              small
              kind="danger"
              label="Remove prompt"
              onPress={() =>
                setDraft({ ...draft, templates: draft.templates.filter((_, n) => n !== i) })
              }
            />
          ) : null}
        </Panel>
      ))}
      {error ? <Notice error>{error}</Notice> : null}
      {canWrite ? (
        <Row>
          <Button
            small
            label="Add prompt"
            onPress={() =>
              setDraft({ ...draft, templates: [...draft.templates, { name: "", text: "" }] })
            }
          />
          <Button
            kind="primary"
            label="Save project settings"
            disabled={busy || draft.templates.some((t) => !t.name.trim() || !t.text.trim())}
            onPress={async () => {
              const ok = await save(
                {
                  ...draft,
                  columns: columns
                    .split("\n")
                    .map((c) => c.trim())
                    .filter(Boolean),
                },
                base,
              );
              if (ok) {
                setSaved(true);
                setBase(revision + 1);
              }
            }}
          />
          <Button
            small
            label="Reload saved settings"
            onPress={() => {
              setDraft(initial);
              setColumns(initial.columns.join("\n"));
              setBase(revision);
              setSaved(false);
            }}
          />
        </Row>
      ) : null}
      {saved ? (
        <Label tone="accent" size={13}>
          Settings saved
        </Label>
      ) : null}
    </View>
  );
}
