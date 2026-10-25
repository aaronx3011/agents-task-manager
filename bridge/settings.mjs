// Bridge settings from the environment (issue #16). Worker credentials are
// worker-scoped (the bound agent's Bearer key), never a shared dashboard token.
import { createHash } from "node:crypto"
import { hostname } from "node:os"
import { fileURLToPath } from "node:url"
import { realpathSync } from "node:fs"

export function workerSettings(options) {
  const endpoint = (value, label) => {
    if (!URL.canParse(value)) throw new Error(`${label} must be an absolute HTTP(S) URL`)
    const url = new URL(value)
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new Error(`${label} must be HTTP(S) without credentials, query or fragment`)
    }
    return url
  }
  const url = endpoint(options.url ?? "http://127.0.0.1:4096", "OPENCODE_URL")
  if (!options.worker?.trim() || !options.key?.trim() || !options.coordinatorURL?.trim()) {
    throw new Error("Coordinator mode requires WORKER_ID, COORDINATOR_API_KEY and COORDINATOR_URL")
  }
  const coordinator = endpoint(options.coordinatorURL, "COORDINATOR_URL")
  const path = options.syncPath || "/api/worker/sync"
  const syncURL = URL.canParse(path) ? new URL(path) : new URL(path, coordinator.href)
  if (syncURL.origin !== coordinator.origin || syncURL.username || syncURL.password || syncURL.search || syncURL.hash) {
    throw new Error("COORDINATOR_SYNC_PATH must stay on the coordinator origin without credentials, query or fragment")
  }
  const model = parseModel(options.model)
  const limits = Object.fromEntries(
    [["interval", 5000], ["sessionLimit", 50], ["messageLimit", 30]].map(([key, fallback]) => {
      const value = Number(options[key] ?? fallback)
      if (!Number.isSafeInteger(value) || value < 1) throw new Error(`${key} must be a positive integer`)
      if ((key === "sessionLimit" && value > 100) || (key === "messageLimit" && value > 300)) {
        throw new Error(`${key} exceeds the Django sync window limit`)
      }
      return [key, value]
    }),
  )
  const scope = createHash("sha256").update(`${syncURL.href}|${options.worker}`).digest("hex").slice(0, 24)
  return {
    ...options,
    // OpenCode canonicalizes workspace paths (notably /var -> /private/var on macOS).
    directory: options.directory ? realpathSync(options.directory) : undefined,
    model,
    worker: options.worker,
    ...limits,
    url: url.href,
    coordinator: coordinator.href,
    syncURL: syncURL.href,
    // Outstanding intent files live 0700 next to the Django repo, not in cloud.
    stateDirectory:
      options.stateDirectory ?? fileURLToPath(new URL(`../.runtime/workers/${scope}/`, import.meta.url)),
  }
}

function parseModel(value) {
  if (!value) return undefined
  const [providerID, ...rest] = value.split("/")
  if (!providerID || rest.length === 0) throw new Error("WORKER_MODEL must be providerID/modelID")
  return { providerID, modelID: rest.join("/") }
}

export function workerOptions(env = process.env) {
  return {
    worker: env.WORKER_ID,
    key: env.COORDINATOR_API_KEY,
    coordinatorURL: env.COORDINATOR_URL,
    syncPath: env.COORDINATOR_SYNC_PATH,
    url: env.OPENCODE_URL,
    directory: env.OPENCODE_DIRECTORY,
    createDirectory: env.OPENCODE_CREATE_DIRECTORY,
    model: env.WORKER_MODEL,
    password: env.OPENCODE_SERVER_PASSWORD,
    username: env.OPENCODE_SERVER_USERNAME,
    stateDirectory: env.WORKER_STATE_DIRECTORY,
    interval: env.POLL_INTERVAL_MS,
    sessionLimit: env.SESSION_LIMIT,
    messageLimit: env.MESSAGE_LIMIT,
    host: hostname(),
  }
}
