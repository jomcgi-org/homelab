import { LOCATION, PUBLIC_BASE } from "$lib/public/seo.js";
import manifest from "$lib/public/posts/posts-manifest.json";

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
- [Notes](${PUBLIC_BASE}/app/notes): browse my public knowledge graph
- [Home](${PUBLIC_BASE}/): overview and homelab status

## Scotland connector

- [Scotland connector](${PUBLIC_BASE}/scotland): free, read-only MCP server and REST API that ranks road-accessible dark-sky sites in Scotland by forecast clear, dark hours over the next seven nights. Pass coordinates; no sign-up or key.
- MCP endpoint: ${PUBLIC_BASE}/scotland/mcp (Streamable HTTP, tool \`dark_sky_windows\`)
- [OpenAPI spec](${PUBLIC_BASE}/scotland/v1/openapi.json): the REST contract
- [Method](${PUBLIC_BASE}/scotland/v1/method): how windows are scored, and the data sources to credit
`;

const ELSEWHERE = `## Elsewhere

- LinkedIn: https://www.linkedin.com/in/jomcgi/
- GitHub: https://github.com/jomcgi
- Email: joe@jomcgi.dev
`;

export function GET() {
  const blog = manifest.length
    ? `\n## Blog\n\n${[...manifest]
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

  return new Response(body, {
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "public, max-age=86400",
    },
  });
}
