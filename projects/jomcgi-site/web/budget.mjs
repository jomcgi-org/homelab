// Fails when a page's initial JavaScript is over budget (HOME-174 performance
// contract). Initial JS is the page's module entry plus every chunk it
// preloads; dynamic imports load on intent and are not counted.
// Usage: node budget.mjs <dist directory>
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";

export const BUDGET_BYTES = 150 * 1024;

export function initialScripts(html) {
  const pattern =
    /<(?:script[^>]*\stype="module"[^>]*\ssrc|link[^>]*\srel="modulepreload"[^>]*\shref)="([^"]+)"/g;
  return [...new Set([...html.matchAll(pattern)].map((match) => match[1]))];
}

export function measure(dist) {
  return readdirSync(dist)
    .filter((name) => name.endsWith(".html"))
    .map((page) => {
      const html = readFileSync(join(dist, page), "utf8");
      const bytes = initialScripts(html)
        .map(
          (src) =>
            gzipSync(readFileSync(join(dist, src.replace(/^\//, "")))).length,
        )
        .reduce((sum, size) => sum + size, 0);
      return { page, bytes };
    });
}

export function overBudget(results) {
  return results.filter(({ bytes }) => bytes > BUDGET_BYTES);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const dist = process.argv[2];
  if (!dist) {
    console.error("usage: node budget.mjs <dist directory>");
    process.exit(2);
  }
  const results = measure(dist);
  if (results.length === 0) {
    console.error(`no HTML pages in ${dist}`);
    process.exit(1);
  }
  const over = new Set(overBudget(results).map(({ page }) => page));
  for (const { page, bytes } of results) {
    console.log(
      `${over.has(page) ? "OVER " : "ok   "} ${page}: ${(bytes / 1024).toFixed(1)} KiB gzipped JS (budget ${BUDGET_BYTES / 1024} KiB)`,
    );
  }
  process.exit(over.size > 0 ? 1 : 0);
}
