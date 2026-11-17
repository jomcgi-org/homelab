import { randomBytes } from "node:crypto";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  BUDGET_BYTES,
  htmlPages,
  initialScripts,
  measure,
  overBudget,
} from "../budget.mjs";

// Random bytes do not compress, so 200 KiB stays over 150 KiB gzipped.
const big = () => randomBytes(200 * 1024);

function site(pages: Record<string, string>) {
  const dist = mkdtempSync(join(tmpdir(), "budget-"));
  mkdirSync(join(dist, "assets"));
  writeFileSync(join(dist, "assets/big.js"), big());
  writeFileSync(join(dist, "assets/small.js"), "export const ok = 1;\n");
  for (const [path, html] of Object.entries(pages)) {
    mkdirSync(join(dist, path, ".."), { recursive: true });
    writeFileSync(join(dist, path), html);
  }
  return dist;
}

const over = (dist: string) =>
  overBudget(measure(dist))
    .map((result) => result.page)
    .sort();

describe("JS budget check", () => {
  it("counts the module entry and its preloads, not lazy chunks", () => {
    const html = `
      <script type="module" crossorigin src="/assets/home-abc.js"></script>
      <link rel="modulepreload" crossorigin href="/assets/vendor-def.js">
      <link rel="stylesheet" href="/assets/home-123.css">`;
    expect(initialScripts(html)).toEqual({
      files: ["/assets/home-abc.js", "/assets/vendor-def.js"],
      inline: [],
    });
  });

  it("finds scripts in any attribute order, quoting or form", () => {
    const html = `
      <script src="/assets/a.js" type="module"></script>
      <script src='/assets/b.js'></script>
      <script type=text/javascript src=/assets/c.js></script>
      <link href="/assets/d.js" crossorigin rel="modulepreload">
      <link as="script" rel="preload" href="/assets/e.js">
      <script>window.ready = true;</script>
      <script type="application/ld+json">{"not": "code"}</script>`;
    expect(initialScripts(html)).toEqual({
      files: [
        "/assets/a.js",
        "/assets/b.js",
        "/assets/c.js",
        "/assets/d.js",
        "/assets/e.js",
      ],
      inline: ["window.ready = true;"],
    });
  });

  it("is 150 KiB gzipped per page", () => {
    expect(BUDGET_BYTES).toBe(150 * 1024);
  });

  it("fails over-budget pages at any depth and passes a small one", () => {
    const dist = site({
      "heavy.html": '<script type="module" src="/assets/big.js"></script>',
      "light.html": '<script type="module" src="/assets/small.js"></script>',
      // A section page in a subdirectory, as later sections add (/hikes/).
      "hikes/index.html":
        '<script type="module" src="../assets/big.js"></script>',
    });
    expect(htmlPages(dist).sort()).toEqual([
      "heavy.html",
      "hikes/index.html",
      "light.html",
    ]);
    expect(over(dist)).toEqual(["heavy.html", "hikes/index.html"]);
  });

  it("counts classic, reordered and inline scripts against the budget", () => {
    const dist = site({
      "classic.html": '<script src="/assets/big.js"></script>',
      "reordered.html": '<script src="/assets/big.js" type="module"></script>',
      "inline.html": `<script>${big().toString("base64")}</script>`,
      "data.html": `<script type="application/json">${big().toString("base64")}</script>`,
    });
    expect(over(dist)).toEqual([
      "classic.html",
      "inline.html",
      "reordered.html",
    ]);
  });

  it("refuses a page that loads script from another origin", () => {
    const dist = site({
      "cdn.html": '<script src="https://cdn.example/x.js"></script>',
    });
    expect(() => measure(dist)).toThrow(/external script/);
  });
});
