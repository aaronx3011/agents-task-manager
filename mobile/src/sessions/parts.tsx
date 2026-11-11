import { useState } from "react";
import { View } from "react-native";
import { ApiError } from "@/api/client";
import type { Interaction, Message, Worker } from "@/api/types";
import { useAccess } from "@/auth/access";
import { modelKey } from "@/sessions/api";
import type { CommandQueue } from "@/sessions/api";
import { Button, Disclosure, Field, Label, Notice, Panel, Picker, Row, Toggle } from "@/ui/kit";
import { age } from "@/ui/format";
import { Markdown } from "@/ui/markdown";
import { usePalette } from "@/ui/theme";

export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function pretty(value: unknown) {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

export function ModelPicker({
  worker,
  value,
  onChange,
  disabled,
}: {
  worker: Worker;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <Picker
      label="Model"
      value={value}
      disabled={disabled}
      options={[
        {
          value: "",
          label: `Worker default${worker.default_model ? ` · ${worker.default_model.modelID}` : ""}`,
        },
        ...worker.models.map((model) => ({
          value: modelKey(model),
          label: `${model.providerID} / ${model.name || model.modelID}`,
        })),
      ]}
      onChange={onChange}
    />
  );
}

export function CommandFeedback({ queue }: { queue: CommandQueue }) {
  const { canWrite } = useAccess();
  if (!canWrite)
    return <Notice>Viewers can watch sessions; members send prompts and answers.</Notice>;
  if (queue.isPending) return <Label tone="secondary">Submitting command…</Label>;
  if (queue.error)
    return (
      <Notice error>
        {queue.error.message}
        {!(queue.error instanceof ApiError) || queue.error.status === 0
          ? " Delivery may already be queued. Retrying the unchanged request reuses its idempotency key."
          : ""}
      </Notice>
    );
  if (queue.isSuccess)
    return (
      <Label tone="accent" size={13}>
        Command #{queue.data.id} queued. Delivery and execution status appear below.
      </Label>
    );
  return null;
}

function Code({ children }: { children: string }) {
  return <Markdown text={"```\n" + children + "\n```"} size={13} />;
}

function MessagePart({ value }: { value: unknown }) {
  const part = record(value);
  if (part.type === "text" && typeof part.text === "string") return <Markdown text={part.text} />;
  if (part.type === "reasoning")
    return (
      <Disclosure title="Reasoning">
        <Code>{pretty(part.text)}</Code>
      </Disclosure>
    );
  if (part.type === "tool") {
    const state = record(part.state);
    return (
      <Disclosure
        title={`Tool · ${String(part.tool || "unknown")} · ${String(state.status || "unknown")}`}
      >
        <Label tone="muted" size={12} bold>
          INPUT
        </Label>
        <Code>{pretty(state.input)}</Code>
        {state.output !== undefined ? (
          <>
            <Label tone="muted" size={12} bold>
              OUTPUT
            </Label>
            <Code>{pretty(state.output)}</Code>
          </>
        ) : null}
        {state.error !== undefined ? <Notice error>{pretty(state.error)}</Notice> : null}
      </Disclosure>
    );
  }
  return (
    <Disclosure title={String(part.type || "Message part")}>
      <Code>{pretty(part)}</Code>
    </Disclosure>
  );
}

export function MessageView({ message }: { message: Message }) {
  const palette = usePalette();
  const user = message.role === "user";
  return (
    <View
      style={{
        borderWidth: 1,
        borderRadius: 10,
        padding: 12,
        gap: 8,
        borderColor: palette.line,
        backgroundColor: user ? palette.raised : palette.panel,
      }}
    >
      <Row style={{ justifyContent: "space-between" }}>
        <Label bold tone={user ? "accent" : "text"}>
          {message.role || "message"}
        </Label>
        <Label tone="muted" size={12}>
          {age(message.publishedAt)}
        </Label>
      </Row>
      {message.content.error ? <Notice error>{pretty(message.content.error)}</Notice> : null}
      {message.parts.map((part, index) => (
        <MessagePart key={index} value={part} />
      ))}
    </View>
  );
}

export type Draft = { choices: string[][]; custom: string[] };

export function InteractionForm({
  request,
  worker,
  queue,
  disabled,
}: {
  request: Interaction;
  worker: string;
  queue: CommandQueue;
  disabled: boolean;
}) {
  const [draft, setDraft] = useState<Draft>({ choices: [], custom: [] });
  const send = (action: string, payload: Record<string, unknown>) =>
    queue.mutate({
      worker,
      action,
      sessionID: request.sessionID,
      payload: { requestID: request.id, ...payload },
    });
  const waiting =
    disabled && request.replyDispatched ? (
      <Label tone="accent" size={13}>
        Response recorded; awaiting worker reconciliation.
      </Label>
    ) : null;
  if (request.kind === "permission")
    return (
      <Panel>
        <Label size={16} bold>
          {request.title || request.type || "Permission required"}
        </Label>
        {request.description ? <Label tone="secondary">{request.description}</Label> : null}
        {request.patterns ? <Code>{pretty(request.patterns)}</Code> : null}
        <Row>
          <Button
            kind="primary"
            label="Allow once"
            disabled={disabled}
            onPress={() => send("permission.reply", { reply: "once" })}
          />
          <Button
            label="Always allow"
            accessibilityLabel="Always allow matching rules"
            disabled={disabled}
            onPress={() => send("permission.reply", { reply: "always" })}
          />
          <Button
            label="Deny"
            kind="danger"
            disabled={disabled}
            onPress={() => send("permission.reply", { reply: "reject" })}
          />
        </Row>
        {waiting}
      </Panel>
    );
  const questions = request.questions || [];
  const answers = questions.map((question, index) => {
    const selected = draft.choices[index] || [];
    const custom = draft.custom[index]?.trim();
    return custom && question.custom
      ? question.multiple
        ? [...selected, custom]
        : [custom]
      : selected;
  });
  return (
    <Panel>
      <Label size={16} bold>
        Answers needed
      </Label>
      {questions.map((question, index) => (
        <View key={index} style={{ gap: 6 }}>
          <Label bold>{question.header || `Question ${index + 1}`}</Label>
          <Label tone="secondary">{question.text}</Label>
          {question.options.map((option) => {
            const checked =
              (draft.choices[index] || []).includes(option.label) &&
              (question.multiple || !draft.custom[index]);
            return (
              <View key={option.label} style={{ gap: 2 }}>
                <Toggle
                  label={option.label}
                  value={checked}
                  disabled={disabled}
                  onChange={(on) => {
                    const choices = [...draft.choices];
                    choices[index] = question.multiple
                      ? on
                        ? [...(choices[index] || []), option.label]
                        : (choices[index] || []).filter((value) => value !== option.label)
                      : [option.label];
                    const custom = [...draft.custom];
                    if (!question.multiple) custom[index] = "";
                    setDraft({ choices, custom });
                  }}
                />
                {option.description ? (
                  <Label tone="muted" size={12} style={{ marginLeft: 32 }}>
                    {option.description}
                  </Label>
                ) : null}
              </View>
            );
          })}
          {question.custom ? (
            <Field
              label="Custom answer"
              maxLength={200}
              editable={!disabled}
              value={draft.custom[index] || ""}
              onChangeText={(text) => {
                const custom = [...draft.custom];
                custom[index] = text;
                setDraft({ ...draft, custom });
              }}
            />
          ) : null}
        </View>
      ))}
      <Row>
        <Button
          kind="primary"
          label="Submit answers"
          disabled={disabled || !answers.length || answers.some((answer) => !answer.length)}
          onPress={() => send("question.reply", { answers })}
        />
        <Button
          label="Dismiss questions"
          disabled={disabled}
          onPress={() => send("question.reject", {})}
        />
      </Row>
      {waiting}
    </Panel>
  );
}
