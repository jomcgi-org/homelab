import { readFileSync } from "node:fs";
import { expect, test } from "vitest";

const css = readFileSync(
  new URL("./execution-sheet.css", import.meta.url),
  "utf8",
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
