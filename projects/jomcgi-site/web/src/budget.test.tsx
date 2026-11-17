import { randomBytes } from "node:crypto";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  BUDGET_BYTES,
  initialScripts,
  measure,
  overBudget,
} from "../budget.mjs";

describe("JS budget check", () => {
  it("counts the module entry and its preloads, not lazy chunks", () => {
    const html = `
      <script type="module" crossorigin src="/assets/home-abc.js"></script>
      <link rel="modulepreload" crossorigin href="/assets/vendor-def.js">
      <link rel="stylesheet" href="/assets/home-123.css">`;
    expect(initialScripts(html)).toEqual([
      "/assets/home-abc.js",
      "/assets/vendor-def.js",
    ]);
  });

  it("is 150 KiB gzipped per page", () => {
    expect(BUDGET_BYTES).toBe(150 * 1024);
  });

  it("fails a page whose initial JS is over budget, and passes a small one", () => {
    const dist = mkdtempSync(join(tmpdir(), "budget-"));
    mkdirSync(join(dist, "assets"));
    // Random bytes do not compress, so 200 KiB stays over 150 KiB gzipped.
    writeFileSync(join(dist, "assets/big.js"), randomBytes(200 * 1024));
    writeFileSync(join(dist, "assets/small.js"), "export const ok = 1;\n");
    writeFileSync(
      join(dist, "heavy.html"),
      '<script type="module" src="/assets/big.js"></script>',
    );
    writeFileSync(
      join(dist, "light.html"),
      '<script type="module" src="/assets/small.js"></script>',
    );
    const pages = overBudget(measure(dist)).map((result) => result.page);
    expect(pages).toEqual(["heavy.html"]);
  });
});
