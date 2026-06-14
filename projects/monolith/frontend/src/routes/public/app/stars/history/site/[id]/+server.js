import { error, json } from "@sveltejs/kit";
import { STARS_HISTORY_CACHE_CONTROL } from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for one site's 12-month clear-dark-hours breakdown (ADR 009).
// The historical detail card calls this site route
// (/app/stars/history/site/{id}) lazily when it opens, since the bulk /history
// payload no longer carries the per-month map; the fetch to
// /api/stars/history/site/{id} happens here, server-side in the same pod, keeping
// that private surface off the browser. Mirrors history/[month]/+server.js.
export async function GET({ params, fetch }) {
  const res = await fetch(
    `${API_BASE}/api/stars/history/site/${encodeURIComponent(params.id)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(503, "stars history unavailable");
  }

  return json(await res.json(), {
    headers: { "cache-control": STARS_HISTORY_CACHE_CONTROL },
  });
}
