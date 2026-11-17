// Fails when a page's initial JavaScript is over budget (HOME-174 performance
// contract). Initial JS is the page's module entry plus every chunk it
// preloads; dynamic imports load on intent and are not counted.
// Usage: node budget.mjs <dist directory> [required page ...]
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { gzipSync } from "node:zlib";

export const BUDGET_BYTES = 150 * 1024;

// Script types the browser executes. JSON and other data blocks are not code.
const EXECUTABLE = new Set([
  "",
  "module",
  "text/javascript",
  "application/javascript",
]);

/** A tag's attributes, in any order, quoted or not. */
export function attributes(tag) {
  const found = {};
  for (const [, name, double, single, bare] of tag.matchAll(
    /([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g,
  )) {
    found[name.toLowerCase()] = double ?? single ?? bare ?? "";
  }
  return found;
}

/**
 * The JavaScript a page runs or preloads on load: script files (module or
 * classic), preloaded script files, and inline script bodies.
 */
export function initialScripts(html) {
  const files = [];
  const inline = [];
  for (const [, attrs, body] of html.matchAll(
    /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi,
  )) {
    const attr = attributes(attrs);
    if (!EXECUTABLE.has((attr.type ?? "").trim().toLowerCase())) continue;
    if ("src" in attr) files.push(attr.src);
    else if (body.trim()) inline.push(body);
  }
  for (const [, attrs] of html.matchAll(/<link\b([^>]*)>/gi)) {
    const attr = attributes(attrs);
    const rel = (attr.rel ?? "").toLowerCase().split(/\s+/);
    const script =
      rel.includes("modulepreload") ||
      (rel.includes("preload") && (attr.as ?? "").toLowerCase() === "script");
    if (script && attr.href) files.push(attr.href);
  }
  return { files: [...new Set(files)], inline };
}

/** Every HTML page under dist, at any depth (e.g. "hikes/index.html"). */
export function htmlPages(dist, prefix = "") {
  return readdirSync(join(dist, prefix), { withFileTypes: true }).flatMap(
    (entry) => {
      const path = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (entry.isDirectory()) return htmlPages(dist, path);
      return entry.name.endsWith(".html") ? [path] : [];
    },
  );
}

export function measure(dist) {
  return htmlPages(dist).map((page) => {
    const { files, inline } = initialScripts(
      readFileSync(join(dist, page), "utf8"),
    );
    const fileBytes = files.map((src) => {
      // The CSP allows scripts from this origin only, so the budget does too.
      if (/^(?:[a-z]+:)?\/\//i.test(src)) {
        throw new Error(`${page}: external script ${src} is not allowed`);
      }
      const file = src.startsWith("/")
        ? join(dist, src.slice(1))
        : join(dist, dirname(page), src);
      return gzipSync(readFileSync(file)).length;
    });
    const inlineBytes = inline.map((body) => gzipSync(body).length);
    const bytes = [...fileBytes, ...inlineBytes].reduce((a, b) => a + b, 0);
    return { page, bytes };
  });
}

export function overBudget(results) {
  return results.filter(({ bytes }) => bytes > BUDGET_BYTES);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const dist = process.argv[2];
  if (!dist) {
    console.error(
      "usage: node budget.mjs <dist directory> [required page ...]",
    );
    process.exit(2);
  }
  const results = measure(dist);
  for (const required of process.argv.slice(3)) {
    if (!results.some(({ page, bytes }) => page === required && bytes > 0)) {
      console.error(`${required}: missing entry or no initial JavaScript`);
      process.exit(1);
    }
  }
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
