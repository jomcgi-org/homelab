import { error } from "@sveltejs/kit";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for an artifact's raw HTML. The browser fetches
// /artifact/<id>/raw (rerouted from jomcgi.dev/artifact/<id>/raw via the
// apex->public prefix rule in hooks.js); this handler fetches the HTML
// server-side from the backend's /internal/artifact/<id>/raw endpoint and
// forwards it with a strict Content-Security-Policy. The artifact is always
// rendered inside a sandboxed <iframe sandbox="allow-scripts"> in +page.svelte
// so the CSP is a defense-in-depth layer, not the primary sandbox boundary.
// The browser never calls the backend directly.
const CSP_FALLBACK =
  "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'";

export async function GET({ params, fetch }) {
  const res = await fetch(
    `${API_BASE}/internal/artifact/${encodeURIComponent(params.id)}/raw`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "artifact unavailable");
  }
  const csp = res.headers.get("content-security-policy") ?? CSP_FALLBACK;
  return new Response(await res.text(), {
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "content-security-policy": csp,
    },
  });
}
