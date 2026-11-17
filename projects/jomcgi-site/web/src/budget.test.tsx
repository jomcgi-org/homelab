import { describe, expect, it } from "vitest";
// @ts-expect-error budget.mjs is plain JavaScript run by node in CI.
import { BUDGET_BYTES, initialScripts } from "../budget.mjs";

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
});
