// Generate (default) or verify (--check) src/api-client/schema.d.ts from the
// committed ../openapi/schema.json. CI runs --check so the client never drifts.
import { readFile, writeFile } from "node:fs/promises";
import openapiTS, { astToString } from "openapi-typescript";
import * as prettier from "prettier";

const root = new URL("../", import.meta.url);
const schema = new URL("../openapi/schema.json", root);
const target = new URL("src/api-client/schema.d.ts", root);

const banner =
  "// Generated from openapi/schema.json by `npm run api:generate`. Do not edit.\n";
const ast = await openapiTS(JSON.parse(await readFile(schema, "utf8")));
const output = await prettier.format(banner + astToString(ast), {
  parser: "typescript",
  filepath: target.pathname,
});

if (process.argv.includes("--check")) {
  const current = await readFile(target, "utf8").catch(() => "");
  if (current !== output) {
    console.error(
      "src/api-client/schema.d.ts is out of date with openapi/schema.json. " +
        "Run `npm run api:generate --prefix frontend` and commit the result.",
    );
    process.exit(1);
  }
  console.log("API client types match openapi/schema.json");
} else {
  await writeFile(target, output);
  console.log(`Wrote ${target.pathname}`);
}
