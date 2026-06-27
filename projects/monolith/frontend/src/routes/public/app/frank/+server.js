// Serves the self-contained Frank trip page verbatim at /app/frank.
//
// Frank is a static, single-file document (inline CSS + vanilla JS: a
// countdown, collapsible sections, a photo lightbox). Rewriting 1300 lines into
// idiomatic Svelte buys nothing here, so the original HTML is served as-is via a
// raw import. It is intentionally unlisted: no nav entry, no sitemap row, no
// homepage link. Reachable only by knowing the URL.
import html from "./frank.html?raw";

export function GET() {
  return new Response(html, {
    headers: {
      "content-type": "text/html; charset=utf-8",
      // Mostly static; a few minutes of edge caching keeps origin hits near zero
      // while still letting content updates propagate without a redeploy wait.
      "cache-control": "public, max-age=300",
    },
  });
}
