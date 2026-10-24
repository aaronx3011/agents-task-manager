// Local command receipts: write an intent before calling OpenCode. An
// interrupted intent is one-shot *uncertain*, not retryable (issues #12/#16).
import { createHash, randomUUID } from "node:crypto"
import { mkdir, open, readFile, readdir, rename, unlink, writeFile } from "node:fs/promises"
import { join } from "node:path"

export class CommandReceipts {
  constructor(directory) {
    this.directory = directory
  }

  async save(path, receipt) {
    const temporary = `${path}.${randomUUID()}.tmp`
    try {
      await writeFile(temporary, JSON.stringify(receipt), { mode: 0o600 })
      await rename(temporary, path)
    } finally {
      await unlink(temporary).catch((error) => {
        if (error.code !== "ENOENT") throw error
      })
    }
  }

  async pending() {
    await mkdir(this.directory, { recursive: true, mode: 0o700 })
    const entries = await readdir(this.directory)
    const receipts = await Promise.all(
      entries
        .filter((entry) => entry.endsWith(".json"))
        .map(async (entry) => {
          const path = join(this.directory, entry)
          const receipt = JSON.parse(await readFile(path, "utf8"))
          if (!receipt.result) {
            receipt.result = {
              id: receipt.id,
              status: "error",
              error: "Worker stopped during command delivery; outcome uncertain. Not replayed; reconcile with the OpenCode session.",
            }
            await this.save(path, receipt)
          }
          return receipt
        }),
    )
    return receipts.filter((receipt) => !receipt.acknowledged).map((receipt) => receipt.result)
  }

  async acknowledge(results) {
    await Promise.all(
      results.map(async (result) => {
        const path = join(this.directory, `${createHash("sha256").update(result.id).digest("hex")}.json`)
        const receipt = JSON.parse(await readFile(path, "utf8"))
        await this.save(path, { ...receipt, acknowledged: true })
      }),
    )
  }

  async execute(command, run) {
    await mkdir(this.directory, { recursive: true, mode: 0o700 })
    const path = join(this.directory, `${createHash("sha256").update(command.id).digest("hex")}.json`)
    const payload = command.payload ?? {}
    const fingerprint = createHash("sha256")
      .update(
        JSON.stringify([
          command.action,
          command.sessionID ?? payload.sessionID ?? null,
          payload.title ?? null,
          payload.directory ?? null,
          payload.model?.providerID ?? null,
          payload.model?.modelID ?? null,
          payload.text ?? null,
          payload.noReply ?? false,
          command.requestID ?? payload.requestID ?? null,
          payload.reply ?? null,
          payload.message ?? null,
          payload.answers ?? null,
        ]),
      )
      .digest("hex")
    const intent = await open(path, "wx", 0o600).catch((error) => {
      if (error.code !== "EEXIST") throw error
      return null
    })
    if (!intent) {
      const receipt = await readFile(path, "utf8").then(JSON.parse).catch(() => null)
      if (receipt?.fingerprint !== fingerprint || !receipt.result) {
        return {
          id: command.id,
          status: "error",
          error: "Command delivery is uncertain or its payload changed; not replayed. Reconcile with the OpenCode session before issuing a new command.",
        }
      }
      return receipt.result
    }
    try {
      await intent.writeFile(JSON.stringify({ id: command.id, fingerprint }))
      await intent.sync()
    } finally {
      await intent.close()
    }
    const result = await run().then(
      (value) => ({ id: command.id, status: "done", result: value }),
      (error) => ({ id: command.id, status: "error", error: error.message }),
    )
    await this.save(path, { id: command.id, fingerprint, result })
    return result
  }
}
