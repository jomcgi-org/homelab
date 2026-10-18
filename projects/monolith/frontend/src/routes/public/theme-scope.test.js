import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const routeDir = fileURLToPath(new URL(".", import.meta.url));
const sourceDir = fileURLToPath(new URL("../../", import.meta.url));
const layout = readFileSync(new URL("+layout.svelte", import.meta.url), "utf8");
const publicTokens = readFileSync(
  new URL("../../lib/public/styles/design-system.css", import.meta.url),
  "utf8",
);
const sharedTokens = readFileSync(
  new URL("../../lib/styles/shared/tokens.css", import.meta.url),
  "utf8",
);

function declarationsFor(css, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `${selector} must exist in ${routeDir}`).not.toBeNull();
  return new Set(
    [...match[1].matchAll(/(--[a-z0-9-]+)\s*:/g)].map((entry) => entry[1]),
  );
}

function svelteFilesUnder(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = `${dir}/${entry.name}`;
    if (entry.isDirectory()) return svelteFilesUnder(path);
    return entry.name.endsWith(".svelte") ? [path] : [];
  });
}

describe("public theme token scope", () => {
  it("owns every token that conflicts with the shared root defaults", () => {
    const sharedRoot = declarationsFor(sharedTokens, ":root");
    const publicRoot = declarationsFor(publicTokens, ":root");
    const publicScope = declarationsFor(
      publicTokens,
      "body:has(.public-theme)",
    );
    const collisions = [...sharedRoot].filter(
      (token) => publicRoot.has(token) || publicScope.has(token),
    );

    expect(collisions.sort()).toEqual([
      "--accent",
      "--bg",
      "--coral",
      "--cream",
      "--green",
    ]);
    expect(collisions.every((token) => publicScope.has(token))).toBe(true);
    expect(collisions.every((token) => !publicRoot.has(token))).toBe(true);
  });

  it("marks every surface that imports the brutalist design system", () => {
    expect(layout).toContain('<div class="public-theme" hidden></div>');

    const importers = svelteFilesUnder(sourceDir).filter((path) =>
      /^\s*import\s+["'][^"']*design-system\.css["'];/m.test(
        readFileSync(path, "utf8"),
      ),
    );

    // Two importers: the public layout, which renders the brutalist pages
    // and marks the public theme, and the root error boundary, which
    // renders a technical-drawing sheet (no marker: its tokens live under
    // .td) but keeps the import because SvelteKit preloads the root error
    // node on every page and the friends and private tiers rely on the
    // sheet reaching them that way.
    expect(importers.map((path) => relative(sourceDir, path)).sort()).toEqual([
      "routes/+error.svelte",
      "routes/public/+layout.svelte",
    ]);
    const errorPage = readFileSync(
      join(sourceDir, "routes/+error.svelte"),
      "utf8",
    );
    expect(errorPage).not.toContain('<div class="public-theme" hidden></div>');
    expect(errorPage).toContain('<main class="td sheet');
  });
});
