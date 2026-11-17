import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// Every colour on the site comes from these tokens, so checking token pairs
// catches contrast regressions before merge, with no browser. The Kargo dev
// stage still runs axe in a real browser for anything tokens cannot show.

type Mode = "light" | "dark";

function tokens(css: string): Map<string, Record<Mode, string>> {
  const found = new Map<string, Record<Mode, string>>();
  for (const [, name, light, dark] of css.matchAll(
    /(--[a-z-]+):\s*light-dark\((#[0-9a-f]{6}),\s*(#[0-9a-f]{6})\)/gi,
  )) {
    found.set(name!, { light: light!, dark: dark! });
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

const palette = tokens(readFileSync("src/palette.css", "utf8"));

// Every colour token has one role, and each role has its WCAG minimum:
// text 4.5:1 (1.4.3) and non-text UI 3:1 (1.4.11) on every background.
// `on-accent` text sits on the accent fill instead. Decorative borders, such
// as the dividers between list items, have no minimum under 1.4.11.
type Role = "text" | "on-accent" | "non-text" | "background" | "decorative";

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
};

const withRole = (role: Role) =>
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
  it("gives every colour token in palette.css exactly one role", () => {
    expect([...palette.keys()].sort()).toEqual(Object.keys(ROLES).sort());
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
