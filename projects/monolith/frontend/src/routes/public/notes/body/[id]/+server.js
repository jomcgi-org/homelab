import { error, json } from "@sveltejs/kit";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the public note body. The notes panel fetches this site
// route (/notes/body/<id>) when a graph node is selected; the fetch to the
// backend's /api/knowledge/public/notes/<id> happens server-side here, so the
// browser never calls the backend directly. This keeps the public API entirely
// off the internet: the gateway only ever routes to the frontend, and the
// backend is reachable only by the frontend in-cluster (mirrors the ships/stars
// +server.js proxies). The backend's effective_visibility gate + the public_api
// views remain the confidentiality boundary regardless.
export async function GET({ params, fetch }) {
  const res = await fetch(
    `${API_BASE}/api/knowledge/public/notes/${encodeURIComponent(params.id)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    // Preserve the identical-404 semantics (missing and private both 404).
    throw error(res.status === 404 ? 404 : 503, "note unavailable");
  }
  return json(await res.json());
}
