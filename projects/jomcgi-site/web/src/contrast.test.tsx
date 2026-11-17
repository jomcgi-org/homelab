import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Every colour on the site comes from these tokens, so checking token pairs
// catches contrast regressions before merge, with no browser. The Kargo dev
// stage still runs axe in a real browser for anything tokens cannot show.

type Mode = "light" | "dark";
type Colour = Record<Mode, string>;

const HEX = "#(?:[0-9a-f]{3}|[0-9a-f]{6})";

/** "#abc" → "#aabbcc"; "#aabbcc" unchanged. */
function longHex(hex: string): string {
  const digits = hex.slice(1).toLowerCase();
  return digits.length === 3
    ? `#${[...digits].map((d) => d + d).join("")}`
    : `#${digits}`;
}

/**
 * The light and dark values of a colour, or undefined when the value is in a
 * form this check cannot read (rgb(), a named colour, var(), and so on).
 */
export function colour(value: string): Colour | undefined {
  const pair = value.match(
    new RegExp(`^light-dark\\(\\s*(${HEX})\\s*,\\s*(${HEX})\\s*\\)$`, "i"),
  );
  if (pair) return { light: longHex(pair[1]!), dark: longHex(pair[2]!) };
  const single = value.match(new RegExp(`^(${HEX})$`, "i"));
  if (single) return { light: longHex(single[1]!), dark: longHex(single[1]!) };
  return undefined;
}

/** Every custom property declared in a stylesheet, whatever its value. */
export function customProperties(css: string): Map<string, string> {
  const found = new Map<string, string>();
  for (const [, name, value] of css.matchAll(
    /(--[a-z0-9-]+)\s*:\s*([^;]+);/gi,
  )) {
    found.set(name!, value!.trim());
  }
  return found;
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const channel = parseInt(hex.slice(i, i + 2), 16) / 255;
    return channel <= 0.03928
      ? channel / 12.92
      : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

const declared = customProperties(readFileSync("src/palette.css", "utf8"));

// Every colour token has one role, and each role has its WCAG minimum:
// text 4.5:1 (1.4.3) and non-text UI 3:1 (1.4.11) on every background.
// `on-accent` text sits on the accent fill instead. Decorative borders, such
// as the dividers between list items, have no minimum under 1.4.11.
type ColourRole =
  "text" | "on-accent" | "non-text" | "background" | "decorative";
// Non-colour tokens have a role too, with a strict syntax that no colour
// matches, so a colour cannot hide as one.
type OtherRole = "font" | "radius" | "shadow";
type Role = ColourRole | OtherRole;

const SYNTAX: Record<OtherRole, RegExp> = {
  font: /^(?:"[^"#()]+"|[a-z][a-z0-9-]*)(?:\s*,\s*(?:"[^"#()]+"|[a-z][a-z0-9-]*))*$/i,
  radius: /^(?:0|\d+(?:\.\d+)?(?:px|rem|em|%))$/,
  shadow: /^none$/,
};
const isOther = (role: Role): role is OtherRole => role in SYNTAX;

const ROLES: Record<string, Role> = {
  "--color-accent": "non-text",
  "--color-on-accent": "on-accent",
  "--color-text-accent": "text",
  "--color-icon-accent": "non-text",
  "--color-background-body": "background",
  "--color-background-surface": "background",
  "--color-background-card": "background",
  "--color-background-muted": "background",
  "--color-text-primary": "text",
  "--color-text-secondary": "text",
  "--color-border": "decorative",
  "--color-border-emphasized": "non-text",
  "--focus-outline-color": "non-text",
  "--font-family-body": "font",
  "--font-family-heading": "font",
  "--radius-inner": "radius",
  "--radius-element": "radius",
  "--radius-container": "radius",
  "--radius-page": "radius",
  "--shadow-low": "shadow",
  "--shadow-med": "shadow",
  "--shadow-high": "shadow",
};

const palette = new Map(
  [...declared]
    .filter(([name]) => !isOther(ROLES[name] ?? "text"))
    .map(([name, value]) => [name, colour(value)] as const),
);

const withRole = (role: ColourRole) =>
  Object.keys(ROLES).filter((token) => ROLES[token] === role);
const backgrounds = withRole("background");

const PAIRS: [string, string, number][] = [
  ...withRole("text").flatMap((token) =>
    backgrounds.map((bg): [string, string, number] => [token, bg, 4.5]),
  ),
  ...withRole("non-text").flatMap((token) =>
    backgrounds.map((bg): [string, string, number] => [token, bg, 3]),
  ),
  ...withRole("on-accent").map((token): [string, string, number] => [
    token,
    "--color-accent",
    4.5,
  ]),
];

describe("token contrast", () => {
  it("gives every custom property in palette.css exactly one role", () => {
    expect([...declared.keys()].sort()).toEqual(Object.keys(ROLES).sort());
  });

  it("can read each colour token's light and dark value", () => {
    for (const [name, value] of declared) {
      const role = ROLES[name]!;
      if (isOther(role)) {
        expect(value, `${name} (${role})`).toMatch(SYNTAX[role]);
      } else {
        expect(colour(value), `${name}: ${value}`).toBeDefined();
      }
    }
  });

  it("finds every custom property, so an unlisted colour fails the role check", () => {
    const css = `--warning: #fff; --brand: rgb(0 0 0); --radius-page: 0;`;
    expect([...customProperties(css).keys()]).toEqual([
      "--warning",
      "--brand",
      "--radius-page",
    ]);
    expect(colour("#fff")).toEqual({ light: "#ffffff", dark: "#ffffff" });
    expect(colour("rgb(0 0 0)")).toBeUndefined();
    for (const role of ["font", "radius", "shadow"] as const) {
      expect("#fff").not.toMatch(SYNTAX[role]);
      expect("rgb(0 0 0)").not.toMatch(SYNTAX[role]);
    }
  });

  it("keeps colours out of every other stylesheet", () => {
    const LITERAL =
      /#[0-9a-f]{3,8}\b|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color|color-mix|light-dark)\(/i;
    const COLOUR_PROPERTY =
      /(?:^|[;{\s])((?:background|border|outline|text-decoration|column-rule)(?:-[a-z]+)*|color|fill|stroke|caret-color|accent-color|box-shadow|text-shadow)\s*:\s*([^;}]+)/gi;
    const ALLOWED = new Set([
      "inherit",
      "initial",
      "unset",
      "currentcolor",
      "transparent",
      "none",
      "solid",
      "dashed",
      "dotted",
      "double",
    ]);
    const sheets = readdirSync("src").filter(
      (name) => name.endsWith(".css") && name !== "palette.css",
    );
    expect(sheets.length).toBeGreaterThan(0);
    for (const sheet of sheets) {
      const css = readFileSync(join("src", sheet), "utf8").replace(
        /\/\*[\s\S]*?\*\//g,
        "",
      );
      expect([...customProperties(css).keys()], sheet).toEqual([]);
      expect(css, sheet).not.toMatch(LITERAL);
      for (const [, property, value] of css.matchAll(COLOUR_PROPERTY)) {
        const words = value!
          .replace(/var\([^()]*(?:\([^()]*\)[^()]*)*\)/g, " ")
          .split(/[\s,]+/)
          .filter((word) => word && !/^-?\d/.test(word));
        for (const word of words) {
          expect(
            ALLOWED.has(word.toLowerCase()),
            `${sheet}: ${property}: ${word}`,
          ).toBe(true);
        }
      }
    }
  });

  it("computes WCAG ratios", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
  });

  for (const mode of ["light", "dark"] as const) {
    it.each(PAIRS)(`${mode}: %s on %s is at least %d:1`, (fg, bg, min) => {
      const foreground = palette.get(fg)?.[mode];
      const background = palette.get(bg)?.[mode];
      expect(foreground, fg).toBeDefined();
      expect(background, bg).toBeDefined();
      expect(contrast(foreground!, background!)).toBeGreaterThanOrEqual(min);
    });
  }
});
