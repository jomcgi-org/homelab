import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const routeDir = fileURLToPath(new URL(".", import.meta.url));
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

  it("activates the scope only while the public layout is mounted", () => {
    expect(layout).toContain('<div class="public-theme" hidden></div>');
  });
});
