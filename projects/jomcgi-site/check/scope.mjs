import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

export const JUNK_PATH = "/__site-check_missing_page_7e86b5";

// Paths the retired hub served (HOME-261). The Worker has no page or redirect
// for them, so each must answer the site's 404 page wherever the stage routes
// it to the Worker. Trips is not here: it is a disabled section that can
// come back (data/sources.json). The release ships no Ships page until
// HOME-411, which takes /app/ships off this list.
export const RETIRED_PATHS = [
  "/api/anything",
  "/functions/x",
  "/grimoire",
  "/app/grimoire",
  "/app/ships",
  "/app/trips",
  "/health",
  "/notes",
];

export function scopeFor(stage) {
  if (stage === "dev")
    return { owns: () => true, junkPaths: [JUNK_PATH, ...RETIRED_PATHS] };
  assert.equal(stage, "prod", "unknown site-check stage");
  // Use the same manifest as the edge Job. A path outside its patterns is
  // not this Worker's, so its page need not match this release.
  const routes = JSON.parse(
    readFileSync(new URL("../edge/routes.json", import.meta.url)),
  );
  const paths = routes.patterns.map((pattern) => {
    assert.ok(pattern.startsWith(`${routes.zone}/`), "route outside site zone");
    const path = pattern.slice(routes.zone.length);
    assert.match(
      path,
      /^\/[^*]*\*?$/,
      "only trailing route wildcards supported",
    );
    return path;
  });
  const owns = (path) =>
    paths.some((route) =>
      route.endsWith("*")
        ? path.startsWith(route.slice(0, -1))
        : path === route,
    );
  const junkPaths = paths
    .filter(
      (path) =>
        path.endsWith("*") &&
        path !== "/assets/*" &&
        !path.startsWith("/data/"),
    )
    // Fill the wildcard without adding a slash. A legacy prefix can redirect
    // every descendant (for example /app/llm-leaderboard/*) to a real page.
    .map((path) => path.slice(0, -1) + JUNK_PATH.slice(1))
    .concat(RETIRED_PATHS.filter(owns));
  assert.ok(
    junkPaths.length,
    "production needs a page route for its 404 check",
  );
  return { owns, junkPaths };
}
