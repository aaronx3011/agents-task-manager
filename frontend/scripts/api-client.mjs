// Generate (default) or verify (--check) the TypeScript API types for every
// client from the committed ../openapi/schema.json: the web SPA and the Expo
// app (#54) share one generator so they never disagree. CI runs --check.
import { mkdir, readFile, writeFile } from "node:fs/promises";
import openapiTS, { astToString } from "openapi-typescript";
import * as prettier from "prettier";

const root = new URL("../", import.meta.url);
const schema = new URL("../openapi/schema.json", root);
const targets = [
  new URL("src/api-client/schema.d.ts", root),
  new URL("../mobile/src/api/schema.d.ts", root),
];

const banner =
  "// Generated from openapi/schema.json by `npm run api:generate --prefix frontend`. Do not edit.\n";
const ast = await openapiTS(JSON.parse(await readFile(schema, "utf8")));
const output = await prettier.format(banner + astToString(ast), {
  parser: "typescript",
  filepath: targets[0].pathname,
});

if (process.argv.includes("--check")) {
  let stale = false;
  for (const target of targets) {
    const current = await readFile(target, "utf8").catch(() => "");
    if (current !== output) {
      console.error(
        `${target.pathname} is out of date with openapi/schema.json.`,
      );
      stale = true;
    }
  }
  if (stale) {
    console.error(
      "Run `npm run api:generate --prefix frontend` and commit the result.",
    );
    process.exit(1);
  }
  console.log("API client types match openapi/schema.json");
} else {
  for (const target of targets) {
    await mkdir(new URL(".", target), { recursive: true });
    await writeFile(target, output);
    console.log(`Wrote ${target.pathname}`);
  }
}
