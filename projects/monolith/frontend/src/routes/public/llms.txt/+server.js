import { PUBLIC_BASE } from "$lib/public/seo.js";

// /llms.txt (llmstxt.org convention): a markdown digest an LLM can read to
// understand who this is and what to cite, without scraping every page. Kept
// terse and factual; the canonical detail lives on the linked pages.
const BODY = `# Joe McGinley

> Senior Platform Engineer at Semgrep, based in Vancouver. Runs Kubernetes hands-on from ingress to eBPF: controllers, CRDs, observability, and per-customer cost attribution, across AWS and GCP. Currently open to senior platform / infrastructure / reliability roles.

## Profile

- Role: Senior Platform Engineer @ Semgrep (May 2025 to present)
- Focus: Kubernetes, platform engineering, eBPF, observability (OpenTelemetry), reliability, distributed systems
- Cloud: AWS / EKS and Google Cloud / GKE
- Location: Vancouver, BC, Canada

## Pages

- [CV](${PUBLIC_BASE}/cv): full work history and project case studies
- [Notes](${PUBLIC_BASE}/notes): public knowledge graph
- [Homelab / SLOs](${PUBLIC_BASE}/slos): live system topology and reliability targets
- [Home](${PUBLIC_BASE}/): overview and homelab status

## Elsewhere

- LinkedIn: https://www.linkedin.com/in/jomcgi/
- GitHub: https://github.com/jomcgi
- Email: joe@jomcgi.dev
`;

export function GET() {
  return new Response(BODY, {
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "public, max-age=86400",
    },
  });
}
