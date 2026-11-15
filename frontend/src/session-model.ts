import type { Message, ModelChoice } from "./opencode-types";

export function modelKey(model: ModelChoice | null | undefined): string {
  return model ? `${model.providerID}/${model.modelID}` : "";
}

function readModel(value: unknown): ModelChoice | null {
  if (!value || typeof value !== "object") return null;
  const model = value as Record<string, unknown>;
  return typeof model.providerID === "string" &&
    model.providerID &&
    typeof model.modelID === "string" &&
    model.modelID
    ? { providerID: model.providerID, modelID: model.modelID }
    : null;
}

// OpenCode session inventory can omit the model. User messages carry a nested
// model; assistant messages carry providerID/modelID directly. The merged chat
// timeline is chronological and includes both saved history and live updates.
export function conversationModel(
  messages: Message[],
  fallback: ModelChoice | null | undefined,
): ModelChoice | null {
  for (let index = messages.length - 1; index >= 0; index--) {
    const message = messages[index];
    if (message.content.summary === true) continue;
    const model =
      message.role === "user"
        ? readModel(message.content.model)
        : message.role === "assistant"
          ? readModel(message.content)
          : null;
    if (model) return model;
  }
  return readModel(fallback);
}

export function composerModel(
  override: string | undefined,
  observed: ModelChoice | null,
): string {
  return override ?? modelKey(observed);
}

export function modelPayload(
  value: string,
  models: ModelChoice[],
  observed?: ModelChoice | null,
) {
  const selected = [...models, ...(observed ? [observed] : [])].find(
    (model) => modelKey(model) === value,
  );
  return selected
    ? { model: { providerID: selected.providerID, modelID: selected.modelID } }
    : {};
}
