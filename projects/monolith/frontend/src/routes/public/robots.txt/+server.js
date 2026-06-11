import { PUBLIC_BASE } from "$lib/public/seo.js";

// Served to crawlers at https://jomcgi.dev/robots.txt. The reroute hook
// rewrites that host path to /public/robots.txt, which resolves here. A plain
// file in static/ would serve at /robots.txt and never be hit by the rewrite.
//
// Policy: allow everything (training and retrieval bots alike). The public CV
// content is published intentionally; the goal is maximum discoverability for
// AI-assisted candidate research.
const BODY = `User-agent: *
Allow: /

Sitemap: ${PUBLIC_BASE}/sitemap.xml
`;

export function GET() {
  return new Response(BODY, {
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "public, max-age=86400",
    },
  });
}
