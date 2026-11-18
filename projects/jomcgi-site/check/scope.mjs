import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

export const JUNK_PATH = "/__site-check_missing_page_7e86b5";

export function scopeFor(stage) {
  if (stage === "dev") return { owns: () => true, junkPaths: [JUNK_PATH] };
  assert.equal(stage, "prod", "unknown site-check stage");
  // Use the same manifest as the edge Job. The hub still owns other paths
  // during migration; its pages need not match this Worker's release.
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
    .map((path) => path.slice(0, -1) + JUNK_PATH.slice(1));
  assert.ok(
    junkPaths.length,
    "production needs a page route for its 404 check",
  );
  return { owns, junkPaths };
}
