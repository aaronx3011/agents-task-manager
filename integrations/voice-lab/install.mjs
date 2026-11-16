// Install the server-owned policy into the separately operated Voice Lab.
// Fail on unfamiliar source instead of overwriting a modified backend blindly.
import { copyFile, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

export function configureServer(source) {
  const imported =
    "import { system, policyVersion, policyHash } from './pm-policy.mjs';";
  if (!source.includes(imported)) {
    const previous = source.match(
      /^const system = 'You are a friendly voice assistant\.[^\n]+;$/m,
    )?.[0];
    if (!previous)
      throw new Error(
        "Unrecognized Voice Lab system prompt; inspect server.mjs before installing",
      );
    source = source.replace(previous, imported);
  }
  const oldStatus =
    "modelReady:tags?.models?.some(item => item.name === model) ?? false, mcp:mcp.status()";
  const newStatus =
    "modelReady:tags?.models?.some(item => item.name === model) ?? false, policy:{version:policyVersion,sha256:policyHash}, mcp:mcp.status()";
  if (!source.includes(newStatus)) {
    if (!source.includes(oldStatus))
      throw new Error(
        "Unrecognized Voice Lab status response; inspect server.mjs before installing",
      );
    source = source.replace(oldStatus, newStatus);
  }
  if (!source.includes("{role:'system',content:system}"))
    throw new Error("Voice Lab no longer injects the server system prompt");
  return source;
}

export async function install(directory) {
  const server = resolve(directory, "server.mjs");
  const source = await readFile(server, "utf8");
  const configured = configureServer(source);
  await copyFile(
    new URL("./pm-policy.mjs", import.meta.url),
    resolve(directory, "pm-policy.mjs"),
  );
  if (configured !== source) await writeFile(server, configured);
}

if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href
) {
  if (!process.argv[2])
    throw new Error(
      "Usage: node integrations/voice-lab/install.mjs /path/to/voice-assistant-backend",
    );
  await install(fileURLToPath(pathToFileURL(resolve(process.argv[2]))));
  console.log(
    "PM router policy installed. Restart Voice Lab and verify /api/status policy version/hash.",
  );
}
