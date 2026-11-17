// Fails when a built page was not pre-rendered: its marker is still there, or
// its root holds no markup. Search engines and link previews read this HTML.
// Usage: node prerendered.mjs <dist directory>
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { htmlPages } from "./budget.mjs";

/** True when the page's root element holds rendered markup and no marker. */
export function prerendered(html) {
  return !html.includes("<!--ssr-->") && /<div id="root">\s*<\w/.test(html);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const dist = process.argv[2];
  let failed = false;
  for (const page of htmlPages(dist)) {
    const ok = prerendered(readFileSync(join(dist, page), "utf8"));
    console.log(`${ok ? "ok  " : "FAIL"}  ${page}`);
    failed ||= !ok;
  }
  process.exit(failed ? 1 : 0);
}
