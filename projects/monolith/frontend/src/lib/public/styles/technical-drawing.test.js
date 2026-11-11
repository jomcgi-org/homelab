import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const css = readFileSync(
  new URL("./technical-drawing.css", import.meta.url),
  "utf8",
);
const packageTheme = readFileSync(
  new URL(
    "../../../../../../design-system/tokens/technical-drawing.css",
    import.meta.url,
  ),
  "utf8",
);

// The declarations of every block whose selector matches `selector` exactly.
function blocks(source, selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return [
    ...source.matchAll(new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`, "g")),
  ].map((match) => match[1]);
}

const declared = (body) =>
  new Set([...body.matchAll(/(--[a-z0-9-]+)\s*:/g)].map((entry) => entry[1]));

const values = (body) =>
  Object.fromEntries(
    [
      ...body
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g),
    ].map((entry) => [entry[1], entry[2].replace(/\s+/g, " ").trim()]),
  );

describe("technical-drawing tokens", () => {
  it("shares only palette declarations with token-only chrome", () => {
    const selectors = [
      ".td-chrome,\n.td",
      '  :root:not([data-theme="light"]) .td-chrome,\n  :root:not([data-theme="light"]) .td',
      ':root[data-theme="dark"] .td-chrome,\n:root[data-theme="dark"] .td',
    ];
    for (const selector of selectors) {
      const [body] = blocks(css, selector);
      expect(body).toBeTruthy();
      expect(body).not.toMatch(
        /(?:^|;)\s*(?:overflow|font-size|position|display)\s*:/,
      );
    }
    expect(css).not.toMatch(/:has\([^)]*td-chrome/);
  });
  it("keeps the two night blocks identical", () => {
    const media = blocks(css, '  :root:not([data-theme="light"]) .td');
    const explicit = blocks(css, ':root[data-theme="dark"] .td');
    expect(media).toHaveLength(1);
    expect(explicit).toHaveLength(1);
    expect(values(media[0])).toEqual(values(explicit[0]));
  });

  it("defines every token the night scheme overrides in the day block", () => {
    const [day] = blocks(css, ".td");
    const [night] = blocks(css, ':root[data-theme="dark"] .td');
    for (const token of declared(night)) {
      expect(declared(day), token).toContain(token);
    }
  });
});

describe("design-system bridge", () => {
  it("derives every --ds-* role the package's technical-drawing theme declares", () => {
    const [light] = blocks(
      packageTheme,
      '[data-ds-theme="technical-drawing-light"][data-ds-theme]',
    );
    const roles = [...declared(light)].filter((token) =>
      token.startsWith("--ds-"),
    );
    expect(roles.length).toBeGreaterThan(20);
    const bridge = blocks(css, ".td")
      .map(declared)
      .reduce((all, set) => new Set([...all, ...set]), new Set());
    for (const role of roles) {
      expect(bridge, role).toContain(role);
    }
  });

  it("points the roles at page tokens, never at new colour values", () => {
    const bridge = blocks(css, ".td").find((body) =>
      body.includes("--ds-surface"),
    );
    const roleValues = values(bridge);
    for (const [role, value] of Object.entries(roleValues)) {
      if (!role.startsWith("--ds-")) continue;
      expect(value, role).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(/i);
    }
    expect(roleValues["--ds-shadow"]).toBe("none");
    expect(roleValues["--ds-radius"]).toBe("0");
    expect(roleValues["--ds-border-weight"]).toBe("1px");
  });
});

describe("sheet vocabulary", () => {
  it("scopes the shared page classes under .sheet, never bare .td", () => {
    for (const cls of [
      "frame",
      "journal",
      "spine",
      "sec-label",
      "index-list",
      "edition",
      "ed-head",
      "ed-lead",
      "ed-body",
      "spec",
      "divided",
      "prose",
    ]) {
      expect(css, cls).toMatch(new RegExp(`\\.sheet \\.${cls}\\b`));
      expect(css, cls).not.toMatch(new RegExp(`\\n\\.td \\.${cls}\\b`));
    }
  });

  it("draws no shadow, radius or gradient on the sheet", () => {
    const sheet = css.slice(css.indexOf(".sheet {"));
    const rules = sheet.replace(/\/\*[\s\S]*?\*\//g, "");
    const valuesOf = (property) =>
      [...rules.matchAll(new RegExp(`${property}\\s*:\\s*([^;]+);`, "g"))].map(
        (match) => match[1].trim(),
      );
    expect(valuesOf("box-shadow").every((value) => value === "none")).toBe(
      true,
    );
    expect(valuesOf("border-radius").every((value) => value === "0")).toBe(
      true,
    );
    expect(rules).not.toMatch(/gradient\(/);
  });
});
