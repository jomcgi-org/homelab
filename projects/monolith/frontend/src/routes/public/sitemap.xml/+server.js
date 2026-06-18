import { PUBLIC_BASE } from "$lib/public/seo.js";

// Crawl manifest for the public tier. Paths are the public-facing URLs
// (PUBLIC_BASE + path), not the gateway-internal /public/* form. /notes is the
// chat front door; /notes/graph is the standalone browsable graph (no per-note
// URLs, so each appears once).
const PAGES = [
  { path: "/", priority: "1.0" },
  { path: "/cv", priority: "0.9" },
  { path: "/notes", priority: "0.7" },
  { path: "/notes/graph", priority: "0.6" },
];

export function GET() {
  const urls = PAGES.map(
    ({ path, priority }) =>
      `  <url>\n    <loc>${PUBLIC_BASE}${path}</loc>\n    <priority>${priority}</priority>\n  </url>`,
  ).join("\n");

  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls}
</urlset>
`;

  return new Response(body, {
    headers: {
      "content-type": "application/xml; charset=utf-8",
      "cache-control": "public, max-age=86400",
    },
  });
}
