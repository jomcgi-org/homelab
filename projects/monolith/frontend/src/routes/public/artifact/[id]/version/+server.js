import { error, json } from "@sveltejs/kit";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the artifact version (ETag string). The hot-reload
// poller in +page.svelte polls this endpoint every 3s to detect when an
// artifact has been updated; when the version changes, the iframe is reloaded
// by bumping a cache-bust query param on the raw URL. Returning no-store
// ensures the poller always reaches the backend and never serves a stale
// version from the edge cache.
export async function GET({ params, fetch }) {
  const res = await fetch(
    `${API_BASE}/internal/artifact/${encodeURIComponent(params.id)}/version`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "artifact unavailable");
  }
  return json(await res.json(), {
    headers: { "cache-control": "no-store" },
  });
}
