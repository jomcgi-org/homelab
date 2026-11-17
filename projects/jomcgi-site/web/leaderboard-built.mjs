// Check the delivered artifact, including Vite's removal of SSR-only imports.
// Usage: node leaderboard-built.mjs <dist directory>
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import axe from "axe-core";
import { Window } from "happy-dom";
import { BUDGET_BYTES, initialScripts, measure } from "./budget.mjs";

const dist = process.argv[2];
assert(dist, "usage: node leaderboard-built.mjs <dist directory>");
const read = (path) => readFileSync(path, "utf8");
const asset = (path) => read(join(dist, path));
const contains = (text, value) =>
  assert(text.includes(value), `missing ${value}`);
const html = asset("llm-leaderboard.html");
assert(!html.includes("<!--ssr-->"), "report must be rendered at build time");
contains(html, 'id="leaderboard-report"');
contains(html, 'aria-label="Ranked models"');
contains(html, 'aria-label="Per-task results"');
assert.equal(
  [...html.matchAll(/class="panel bars"/g)].length,
  4,
  "all four bar charts must be static",
);
contains(html, 'aria-label="Hard-task pass against mean price per task"');
assert(!/\bstyle\s*=/.test(html), "report must not use inline styles");
const redirects = asset("_redirects").trim().split("\n");
for (const path of ["/app/llm-leaderboard", "/app/llm-leaderboard/*"]) {
  assert(
    redirects.includes(`${path} /llm-leaderboard 301`),
    "missing delivered redirect: " + path,
  );
}

const snapshot = JSON.parse(
  read(
    new URL(
      "../../monolith/frontend/src/lib/public/llm-leaderboard/leaderboard.json",
      import.meta.url,
    ),
  ),
);
for (const task of snapshot.tasks) contains(html, task.id);
const { files, inline } = initialScripts(html);
assert.deepEqual(inline, [], "all executable scripts must be external");
const initial = files.map(asset).join("\n");
const deferredFiles = readdirSync(join(dist, "assets")).filter((file) =>
  /^leaderboard-interactive-.*\.js$/.test(file),
);
assert.equal(deferredFiles.length, 1, "missing deferred report chunk");
contains(initial, 'import("./' + deferredFiles[0] + '")');
const deferred = asset(join("assets", deferredFiles[0]));
for (const model of snapshot.models) {
  assert(
    !initial.includes(model.id),
    "snapshot leaked into initial JS: " + model.id,
  );
  contains(deferred, model.id);
}
const budget = measure(dist).find(
  ({ page }) => page === "llm-leaderboard.html",
);
assert(budget && budget.bytes <= BUDGET_BYTES, "leaderboard exceeds JS budget");
console.log(
  `ok   leaderboard: ${(budget.bytes / 1024).toFixed(1)} KiB initial JS <= ${BUDGET_BYTES / 1024} KiB`,
);

// Inspect emitted HTML without running the page or fetching any resources.
// happy-dom checks structure; contrast.test.tsx checks palette contrast.
const window = new Window({
  settings: {
    enableJavaScriptEvaluation: false,
    disableJavaScriptFileLoading: true,
    enableCSSFileLoading: false,
  },
});
try {
  window.document.write(html);
  const { violations } = await axe.run(window.document.documentElement, {
    rules: { "color-contrast": { enabled: false } },
  });
  assert.deepEqual(violations, [], "structural axe violations in built HTML");
} finally {
  await window.happyDOM.close();
}
console.log(
  "ok   leaderboard: static charts, rankings, tasks and 301 redirects; deferred snapshot/code; zero structural axe violations",
);
