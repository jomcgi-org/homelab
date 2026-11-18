// Validate the emitted page with the same summary stamping used at deploy.
// Usage: node stars-built.mjs dist
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { Window } from "happy-dom";
import axe from "axe-core";
import { initialScripts, measure, BUDGET_BYTES } from "./budget.mjs";
import { makeSummary, stampSummary } from "../data/stars-summary.mjs";

const dist = process.argv[2];
assert.ok(dist, "usage: node stars-built.mjs dist");
const built = await readFile(join(dist, "stars.html"), "utf8");
assert.doesNotMatch(built, /<!--ssr-->/);
const asOf = "2026-11-17T12:00:00.000Z";
const html = stampSummary(
  built,
  makeSummary(
    {
      fetched_at: asOf,
      sites: [{ name: "Galloway", lat: 55, lon: -4, clear_dark_hours: 8 }],
    },
    Date.parse(asOf),
  ),
);
const window = new Window({
  url: "https://jomcgi.dev/stars",
  settings: {
    disableJavaScriptFileLoading: true,
    disableJavaScriptEvaluation: true,
    disableCSSFileLoading: true,
  },
});
try {
  window.document.write(html);
  const document = window.document;
  assert.equal(document.querySelectorAll("h1").length, 1);
  assert.equal(
    document.querySelector("h1").textContent,
    "Dark-sky stargazing map, Scotland viewing windows",
  );
  assert.equal(document.querySelector("#stars-summary time").dateTime, asOf);
  assert.match(
    document.querySelector("#stars-summary").textContent,
    /Galloway — 8 clear dark hours/,
  );
  assert.equal(
    document.querySelector('link[rel="canonical"]').href,
    "https://jomcgi.dev/stars",
  );
  assert.equal(
    document.querySelector('meta[property="og:site_name"]').content,
    "Joe McGinley",
  );
  for (const key of ["og:title", "twitter:title"]) {
    assert.equal(
      document.querySelector(`meta[property="${key}"],meta[name="${key}"]`)
        .content,
      document.title,
    );
  }
  for (const key of ["og:description", "twitter:description"]) {
    assert.equal(
      document.querySelector(`meta[property="${key}"],meta[name="${key}"]`)
        .content,
      document.querySelector('meta[name="description"]').content,
    );
  }
  const credits = document.querySelector(".stars-credits").textContent;
  for (const source of [
    "OpenStreetMap",
    "OpenFreeMap",
    "MET Norway",
    "CC BY 4.0",
    "Copernicus",
  ])
    assert.ok(credits.includes(source));
  assert.equal(document.querySelectorAll("[style]").length, 0);
  const initial = initialScripts(html);
  assert.equal(initial.inline.length, 0);
  for (const file of initial.files) {
    assert.doesNotMatch(file, /stars-map|maplibre/);
    const code = await readFile(join(dist, file.replace(/^\//, "")), "utf8");
    assert.doesNotMatch(code, /tiles\.openfreemap\.org\/styles\/liberty/);
  }
  for (const link of document.querySelectorAll('link[rel="stylesheet"]'))
    assert.doesNotMatch(link.href, /stars-map/);
  assert.ok(
    measure(dist).find(({ page }) => page === "stars.html").bytes <=
      BUDGET_BYTES,
  );
  const redirects = await readFile(join(dist, "_redirects"), "utf8");
  // The whole file, in order, so that an extra rule fails.
  assert.equal(
    redirects,
    "/factory /factory/ 200\n/factory/* /factory/ 200\n/app/llm-leaderboard /llm-leaderboard 301\n/app/llm-leaderboard/* /llm-leaderboard 301\n/app/hikes /hikes 301\n/app/hikes/* /hikes 301\n/app/stars /stars 301\n/app/stars/* /stars 301\n",
  );
  // Page scripts stay disabled; only the test's axe source is evaluated.
  window.eval(axe.source);
  const result = await window.axe.run(document, {
    rules: { "color-contrast": { enabled: false } },
  });
  assert.equal(
    result.violations.length,
    0,
    JSON.stringify(result.violations.map(({ id }) => id)),
  );
  console.log(
    "stars: built HTML, deploy summary, metadata, deferred map, budget, redirects and structural axe passed",
  );
} finally {
  await window.happyDOM.close();
}
