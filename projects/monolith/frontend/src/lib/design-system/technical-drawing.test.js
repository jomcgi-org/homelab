import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import {
  boundary,
  contrast,
  contrastRows,
  graphicRoles,
  luminance,
  parseRules,
  rolesFor,
  schemes,
  seriesRoles,
  surfaces,
  textRoles,
} from "./technical-drawing.test-helper.js";

const require = createRequire(import.meta.url);
const themePath =
  require.resolve("@homelab/design-system/tokens/technical-drawing.css");
const css = readFileSync(themePath, "utf8");
const contract = readFileSync(
  require.resolve("@homelab/design-system/tokens/contract.css"),
  "utf8",
);
const readme = readFileSync(
  resolve(dirname(themePath), "../README.md"),
  "utf8",
);

// Baseline from main, not from the theme file. Keep every default unchanged.
const defaults = {
  "--ds-surface": "#f3ede1",
  "--ds-surface-raised": "#ffffff",
  "--ds-ink": "#1a1a1a",
  "--ds-ink-muted": "#2a2824",
  "--ds-ink-faint": "#6b6658",
  "--ds-line": "#ddd5c3",
  "--ds-line-strong": "#bcb39e",
  "--ds-accent": "#ffde01",
  "--ds-on-accent": "#1a1a1a",
  "--ds-shadow": "4px 4px 0 var(--ds-ink)",
  "--ds-shadow-raised": "6px 6px 0 var(--ds-ink)",
  "--ds-border-weight": "2px",
  "--ds-font-display": '"Instrument Serif", Georgia, serif',
  "--ds-font-body":
    '"Hanken Grotesk", -apple-system, "Helvetica Neue", sans-serif',
  "--ds-font-mono":
    '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
  "--ds-space-xs": "8px",
  "--ds-space-sm": "12px",
  "--ds-space-md": "16px",
  "--ds-space-lg": "24px",
  "--ds-space-xl": "40px",
  "--ds-space-2xl": "60px",
  "--ds-radius": "8px",
  "--ds-ok": "#00b300",
  "--ds-warn": "#ff9900",
  "--ds-err": "#cc0000",
};

describe("technical-drawing token contract", () => {
  it("preserves every main contract default and both existing exports", () => {
    expect(rolesFor(contract, ":root")).toEqual(defaults);
    expect(require.resolve("@homelab/design-system")).toBe(
      require.resolve("@homelab/design-system/tokens/contract.css"),
    );
  });

  it("declares identical complete role sets at both boundaries", () => {
    const expected = [
      ...Object.keys(defaults),
      "--ds-accent-ink",
      "--ds-focus",
      "--ds-focus-width",
      ...seriesRoles,
    ].sort();
    for (const scheme of schemes) {
      expect(Object.keys(rolesFor(css, boundary(scheme))).sort()).toEqual(
        expected,
      );
    }
  });

  it("contains only the two explicit boundaries and permitted local properties", () => {
    const rules = parseRules(css);
    expect(rules.map((rule) => rule.selector)).toEqual(schemes.map(boundary));
    expect(css.replace(/\/\*[\s\S]*?\*\//g, "")).not.toMatch(
      /@|:root|\*|:has\(|prefers-color-scheme/,
    );
    for (const [index, rule] of rules.entries()) {
      expect(
        Object.fromEntries(
          Object.entries(rule.declarations).filter(
            ([name]) => !name.startsWith("--ds-"),
          ),
        ),
      ).toEqual({
        "color-scheme": schemes[index],
        color: "var(--ds-ink)",
        "background-color": "var(--ds-surface)",
      });
      const roles = rolesFor(css, rule.selector);
      expect(roles["--ds-shadow"]).toBe("none");
      expect(roles["--ds-shadow-raised"]).toBe("none");
      expect(roles["--ds-border-weight"]).toBe("1px");
      expect(roles["--ds-radius"]).toBe("0");
      expect(roles["--ds-focus-width"]).toBe("2px");
      expect(roles["--ds-font-display"]).toBe(roles["--ds-font-body"]);
      expect(roles["--ds-font-body"]).toContain('"Schibsted Grotesk"');
      expect(roles["--ds-font-body"]).not.toMatch(/(^|[,\s])serif\b/);
    }
  });

  it("keeps the five chart series in reference memory-tier order", () => {
    const expected = {
      light: ["#0b6bff", "#0b7f47", "#0e7c86", "#d0262a", "#d100b5"],
      dark: ["#63a8ff", "#3ddc84", "#4dd0e1", "#ff6b6b", "#ff7be5"],
    };
    for (const scheme of schemes) {
      const roles = rolesFor(css, boundary(scheme));
      expect(seriesRoles.map((role) => roles[role])).toEqual(expected[scheme]);
    }
  });

  it("computes WCAG luminance and rejects non-opaque colours", () => {
    expect(luminance("#000000")).toBe(0);
    expect(luminance("#ffffff")).toBe(1);
    expect(contrast("#ffffff", "#000000")).toBe(21);
    expect(contrast("#0a0a0a", "#ffffff")).toBeCloseTo(19.798, 3);
    for (const invalid of ["#fff", "rgba(0,0,0,1)", "#000000ff"]) {
      expect(() => luminance(invalid)).toThrow("Expected opaque");
    }
  });

  it.each(schemes)(
    "meets text and graphic contrast on both %s surfaces",
    (scheme) => {
      const roles = rolesFor(css, boundary(scheme));
      for (const surface of surfaces) {
        for (const role of textRoles) {
          expect(["#97917f", "#6f6d65"]).not.toContain(roles[role]);
          expect(
            contrast(roles[role], roles[surface]),
            `${scheme} ${role} ${surface}`,
          ).toBeGreaterThanOrEqual(4.5);
        }
        for (const role of graphicRoles) {
          expect(
            contrast(roles[role], roles[surface]),
            `${scheme} ${role} ${surface}`,
          ).toBeGreaterThanOrEqual(3);
        }
      }
      expect(
        contrast(roles["--ds-on-accent"], roles["--ds-accent"]),
      ).toBeGreaterThanOrEqual(4.5);
    },
  );

  it("keeps the README contrast table equal to computed ratios to two decimals", () => {
    const table = readme
      .split("<!-- contrast:start -->")[1]
      ?.split("<!-- contrast:end -->")[0];
    expect(table).toBeDefined();
    const themes = schemes.map((scheme) => rolesFor(css, boundary(scheme)));
    expect(table).toContain(
      `| Role | Light sheet \`${themes[0][surfaces[0]]}\` | Light raised \`${themes[0][surfaces[1]]}\` | Dark sheet \`${themes[1][surfaces[0]]}\` | Dark raised \`${themes[1][surfaces[1]]}\` |`,
    );
    const rows = table
      .split("\n")
      .filter((line) => line.startsWith("| `--ds-"));
    expect(rows).toEqual(contrastRows(css));
  });

  it("imports the CSS export and pure helpers with browser globals undefined", async () => {
    const globals = ["window", "document", "localStorage", "matchMedia"];
    try {
      for (const name of globals) vi.stubGlobal(name, undefined);
      vi.resetModules();
      await expect(
        import("@homelab/design-system/tokens/technical-drawing.css"),
      ).resolves.toBeDefined();
      const helper = await import("./technical-drawing.test-helper.js");
      expect(helper.contrastRows(css)).toEqual(contrastRows(css));
      for (const name of globals) expect(globalThis[name]).toBeUndefined();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
