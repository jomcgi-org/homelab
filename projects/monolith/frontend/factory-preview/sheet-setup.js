// Generate within this test invocation, never consume another test's output.
// Only invented, read-only endpoint records reach the production loaders.
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { load as overview } from "../src/routes/public/slop/factory/+page.server.js";
import { load as activity } from "../src/routes/public/slop/factory/activity/+page.server.js";
import { load as context } from "../src/routes/public/slop/factory/context/+page.server.js";
import { payloads } from "./fixtures.js";

export default async function setup(project) {
  const pages = {};
  for (const mode of ["live", "empty", "error"]) {
    const values = payloads(mode);
    for (const [view, load, path] of [
      ["overview", overview, "/slop/factory"],
      ["activity", activity, "/slop/factory/activity"],
      ["context", context, "/slop/factory/context"],
      ["chapter", context, "/slop/factory/context?entity=synthetic-project"],
      ["search", context, "/slop/factory/context?q=Synthetic"],
    ]) {
      pages[`${mode}/${view}`] = await load({
        url: new URL(path, "https://fixture.invalid"),
        setHeaders() {},
        fetch: async (url) => {
          if (!(url in values))
            throw new Error(`Unexpected synthetic endpoint: ${url}`);
          return new Response(JSON.stringify(values[url]), {
            status: mode === "error" ? 503 : 200,
            headers: {
              "content-type": "application/json",
              "cache-control": "public, max-age=13, must-revalidate",
            },
          });
        },
      });
    }
  }
  const directory = join(project.config.root, ".generated");
  mkdirSync(directory, { recursive: true });
  writeFileSync(join(directory, "pages.json"), JSON.stringify(pages));
}
