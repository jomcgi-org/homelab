import { readFileSync } from "node:fs";
import { expect, test } from "vitest";

const css = readFileSync(
  new URL("./execution-sheet.css", import.meta.url),
  "utf8",
);
const shared = readFileSync(
  new URL(
    "../../../../lib/public/styles/technical-drawing.css",
    import.meta.url,
  ),
  "utf8",
);
const rgb = (hex) => hex.match(/\w{2}/g).map((part) => parseInt(part, 16));
const luminance = (color) =>
  color
    .map((c) => c / 255)
    .map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
    .reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
const colors = (role) =>
  [...shared.matchAll(new RegExp(`--${role}: #([0-9a-f]{6});`, "g"))].map(
    (match) => rgb(match[1]),
  );

test("execution roles derive from sheet tokens without another palette", () => {
  expect(css).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(|gradient\(/i);
  for (const declaration of css.matchAll(/box-shadow:\s*([^;]+);/g))
    expect(declaration[1]).toBe("none");
  for (const declaration of css.matchAll(/border-radius:\s*([^;]+);/g))
    expect(declaration[1]).toBe("0");
  expect(css).not.toMatch(
    /border(?:-(?:left|right|top|bottom))?:\s*[2-9]px solid/,
  );
  expect(css).toContain("--font-mono: var(--font-code)");
});
test("console scrolling beats document-sheet specificity without changing drain", () => {
  expect(css).toMatch(
    /body:has\(main\.execution-sheet\)\s*\{\s*overflow: hidden;/,
  );
  expect(css).not.toMatch(
    /body:has\(main\.drain-page\)\s*\{\s*overflow: hidden;/,
  );
  expect(css).toMatch(
    /\.execution-sheet \.execution-frame \.chrome\s*\{\s*position: static;\s*z-index: auto;/,
  );
});

test("night scrims dim with the dark paper token in both scheme selectors", () => {
  for (const selector of [
    'html:not([data-theme="light"]) .td.console',
    'html[data-theme="dark"] .td.console',
  ]) {
    const block = css.slice(css.indexOf(selector)).split("}")[0];
    expect(block).toContain(
      "--scrim-bg: color-mix(in srgb, var(--paper) 65%, transparent)",
    );
  }
});

test("success text on its tinted background clears AA in both schemes", () => {
  const mix =
    Number(css.match(/--ok-soft: color-mix\(in srgb, var\(--ok\) (\d+)%/)[1]) /
    100;
  const sheets = colors("sheet");
  // The shared :root night selectors override .td.console's --ok alias.
  const foregrounds = [colors("tone-ram")[0], ...colors("ok").slice(1)];
  expect(foregrounds).toHaveLength(3);
  for (const [index, foreground] of foregrounds.entries()) {
    const background = foreground.map(
      (c, i) => c * mix + sheets[index][i] * (1 - mix),
    );
    const a = luminance(foreground);
    const b = luminance(background);
    expect(
      (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05),
    ).toBeGreaterThanOrEqual(4.5);
  }
});

test("unfocused input and select boundaries clear 3:1 in both schemes", () => {
  expect(css).toContain("--line-strong: var(--ink-2)");
  const boundaries = colors("ink-2");
  const sheets = colors("sheet");
  // Day, system night and explicit night must all retain visible outlines.
  expect(boundaries).toHaveLength(3);
  expect(sheets).toHaveLength(3);
  for (const [index, boundary] of boundaries.entries()) {
    const a = luminance(boundary);
    const b = luminance(sheets[index]);
    expect(
      (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05),
    ).toBeGreaterThanOrEqual(3);
  }
  for (const file of ["Launcher.svelte", "+page.svelte"]) {
    const component = readFileSync(new URL(file, import.meta.url), "utf8");
    expect(component.match(/  \.box\s*\{[^}]+\}/)[0]).toContain(
      "border: 1px solid var(--line-strong)",
    );
    if (file === "+page.svelte") {
      expect(
        component.match(
          /\.new-panel textarea,\s*\.composer textarea,\s*select\s*\{[^}]+\}/,
        )[0],
      ).toContain("border: 1px solid var(--line-strong)");
    }
  }
});

test("the floating session menu has an ink outline", () => {
  const component = readFileSync(
    new URL("./PaneHeader.svelte", import.meta.url),
    "utf8",
  );
  expect(component.match(/\.session-menu-popover\s*\{[^}]+\}/)[0]).toContain(
    "border: 1px solid var(--ink)",
  );
});

test("walkthrough selection has the sheet's visible left marker", () => {
  const component = readFileSync(
    new URL("./SessionWalkthrough.svelte", import.meta.url),
    "utf8",
  );
  expect(component.match(/\.point\s*\{[^}]+\}/)[0]).toContain(
    "border-left: 2px solid transparent",
  );
  const selected = component.match(/\.point\.on\s*\{[^}]+\}/)[0];
  expect(selected).toContain("border-left-color: var(--accent-ink)");
  expect(selected).toContain("color: var(--accent-ink)");
});

test("run detail styles use flat selectors for browsers without nesting", () => {
  expect(css).not.toMatch(/^\.execution-sheet\s*\{/m);
  expect(css).toContain(".execution-sheet .runview {");
  expect(css).not.toMatch(/\.execution-sheet \d+%/);
});
