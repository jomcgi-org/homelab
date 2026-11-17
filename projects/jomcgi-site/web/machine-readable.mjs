// Ported from the hub public routes. Build-time only; post bodies never enter a browser bundle.
import {
  LOCATION,
  PUBLIC_BASE,
} from "../../monolith/frontend/src/lib/public/seo.js";
import { manifest } from "../../monolith/frontend/src/lib/manifest.js";

// Policy: allow everything (training and retrieval bots alike). The public CV
// content is published intentionally; the goal is maximum discoverability for
// AI-assisted candidate research. Except /slop which is unlisted draft content.
const BODY = `User-agent: *
Allow: /
Disallow: /slop

Sitemap: ${PUBLIC_BASE}/sitemap.xml
`;

export function robots() {
  return BODY;
}

// Crawl manifest for the public tier. Paths are the public-facing URLs
// (PUBLIC_BASE + path), not the gateway-internal /public/* form.
const PAGES = [
  { path: "/", priority: "1.0" },
  { path: "/cv", priority: "0.9" },
  // The docs index; per-doc URLs are reachable from it (listed once).
  { path: "/docs", priority: "0.8" },
  // /blog is deliberately unlisted until the first post is published.
  { path: "/scotland", priority: "0.7" },
  { path: "/scotland/privacy", priority: "0.3" },
  { path: "/scotland/terms", priority: "0.3" },
  { path: "/scotland/support", priority: "0.3" },
];

export function sitemap(posts) {
  const blogPages = posts.length
    ? [
        { path: "/blog", priority: "0.8" },
        ...posts.map(({ slug }) => ({
          path: `/blog/${slug}`,
          priority: "0.7",
        })),
      ]
    : [];
  const urls = [...PAGES, ...blogPages]
    .map(
      ({ path, priority }) =>
        `  <url>\n    <loc>${PUBLIC_BASE}${path}</loc>\n    <priority>${priority}</priority>\n  </url>`,
    )
    .join("\n");

  const body = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${urls}
</urlset>
`;

  return body;
}

// /llms.txt (llmstxt.org convention): a markdown digest an LLM can read to
// understand who this is and what to cite, without scraping every page. Kept
// terse and factual; the canonical detail lives on the linked pages.
const INTRO = `# Joe McGinley

> Senior Platform Engineer at Semgrep, based in ${LOCATION.short}. Runs Kubernetes hands-on from ingress to eBPF: controllers, CRDs, observability, and per-customer cost attribution, across AWS and GCP. Currently open to senior platform / infrastructure / reliability roles.

## Profile

- Role: Senior Platform Engineer @ Semgrep (May 2025 to present)
- Focus: Kubernetes, platform engineering, eBPF, observability (OpenTelemetry), reliability, distributed systems
- Cloud: AWS / EKS and Google Cloud / GKE
- Location: ${LOCATION.short}, ${LOCATION.country}

## Pages

- [CV](${PUBLIC_BASE}/cv): full work history and project case studies
- [Home](${PUBLIC_BASE}/): overview and homelab status

## Scotland connector

- [Scotland connector](${PUBLIC_BASE}/scotland): free, read-only MCP server and REST API for planning and checking Scottish trips. \`plan\` takes a point and up to seven days and returns, for each day, the Walkhighlands walks that fit the weather and the best road-accessible dark-sky sites for the night, each with a go or marginal verdict and its reason, or a note saying why nothing suits. \`check\` takes named walks on dates or points on nights and says whether each is on (go, marginal, no, or unknown past the hourly forecast), when, and what else suits. Pass coordinates; no sign-up or key.
- MCP endpoint: ${PUBLIC_BASE}/scotland/mcp (Streamable HTTP, tools \`plan\` and \`check\`)
- Claude Code plugin: \`claude plugin marketplace add ${PUBLIC_BASE}/scotland/marketplace.json\`, then \`claude plugin install scotland@jomcgi\`
- [OpenAPI spec](${PUBLIC_BASE}/scotland/v1/openapi.json): the REST contract
- [Method](${PUBLIC_BASE}/scotland/v1/method): how verdicts are decided, and the data sources to credit
- [Privacy](${PUBLIC_BASE}/scotland/privacy), [terms](${PUBLIC_BASE}/scotland/terms) and [support](${PUBLIC_BASE}/scotland/support)
`;

const ELSEWHERE = `## Elsewhere

- LinkedIn: https://www.linkedin.com/in/jomcgi/
- GitHub: https://github.com/jomcgi
- Email: joe@jomcgi.dev
`;

export function llms(posts) {
  const blog = posts.length
    ? `\n## Blog\n\n${[...posts]
        .sort(
          (a, b) =>
            b.date.localeCompare(a.date) || a.slug.localeCompare(b.slug),
        )
        .map(
          ({ slug, title, summary }) =>
            `- [${title}](${PUBLIC_BASE}/blog/${slug}): ${summary}`,
        )
        .join("\n")}\n\n`
    : "\n";
  const body = `${INTRO}${blog}${ELSEWHERE}`;

  return body;
}

export function webmanifest() {
  return manifest("jomcgi.dev", "jomcgi");
}
