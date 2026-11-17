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

// [foreground, background, minimum ratio]: 4.5 for text (WCAG 1.4.3),
// 3 for focus rings and component borders (WCAG 1.4.11).
const PAIRS: [string, string, number][] = [
  ...["body", "surface", "card", "muted"].flatMap((background) =>
    ["text-primary", "text-secondary", "text-accent"].map(
      (text): [string, string, number] => [
        `--color-${text}`,
        `--color-background-${background}`,
        4.5,
      ],
    ),
  ),
  ["--color-on-accent", "--color-accent", 4.5],
  ["--focus-outline-color", "--color-background-body", 3],
  ["--color-border-emphasized", "--color-background-body", 3],
];

describe("token contrast", () => {
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
