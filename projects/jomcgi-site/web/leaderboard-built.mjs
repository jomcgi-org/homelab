// Check the delivered artifact, including Vite's removal of SSR-only imports.
// Usage: node leaderboard-built.mjs <dist directory>
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { initialScripts } from "./budget.mjs";

const dist = process.argv[2];
assert(dist, "usage: node leaderboard-built.mjs <dist directory>");
const html = readFileSync(join(dist, "llm-leaderboard.html"), "utf8");
assert(!html.includes("<!--ssr-->"), "report must be rendered at build time");
assert(html.includes('id="leaderboard-report"'), "missing report island");
assert(html.includes('aria-label="Ranked models"'), "missing static rankings");
assert(html.includes('aria-label="Per-task results"'), "missing static matrix");
assert.equal(
  [...html.matchAll(/class="panel bars"/g)].length,
  4,
  "all four bar charts must be static",
);
assert(
  html.includes('aria-label="Hard-task pass against mean price per task"'),
  "missing static scatter",
);
assert(!/\bstyle\s*=/.test(html), "report must not use inline styles");

const snapshot = JSON.parse(
  readFileSync(
    new URL(
      "../../monolith/frontend/src/lib/public/llm-leaderboard/leaderboard.json",
      import.meta.url,
    ),
    "utf8",
  ),
);
for (const task of snapshot.tasks) {
  assert(html.includes(task.id), "missing static task " + task.id);
}
const { files, inline } = initialScripts(html);
assert.deepEqual(inline, [], "all executable scripts must be external");
const initial = files
  .map((file) => readFileSync(join(dist, file), "utf8"))
  .join("\n");
const deferredFiles = readdirSync(join(dist, "assets")).filter((file) =>
  /^leaderboard-interactive-.*\.js$/.test(file),
);
assert.equal(deferredFiles.length, 1, "missing deferred report chunk");
assert(
  initial.includes('import("./' + deferredFiles[0] + '")'),
  "report must load through a dynamic import",
);
const deferred = readFileSync(join(dist, "assets", deferredFiles[0]), "utf8");
for (const model of snapshot.models) {
  assert(
    !initial.includes(model.id),
    "snapshot leaked into initial JS: " + model.id,
  );
  assert(
    deferred.includes(model.id),
    "snapshot missing from deferred chunk: " + model.id,
  );
}
console.log(
  "ok   leaderboard: static charts, rankings and tasks; snapshot and chart code deferred",
);
