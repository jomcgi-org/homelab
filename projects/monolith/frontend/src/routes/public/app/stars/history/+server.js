import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  STARS_HISTORY_CACHE_CONTROL,
} from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the historical layer (ADR 008/009). The page fetches
// this once when it switches to Historical mode; the response is every site's
// full 12-month clear-dark-hours breakdown, which the client then filters by
// month locally (so month switching never hits the network). The fetch to
// /api/stars/history happens here, server-side in the same pod, keeping that
// private surface off the browser. Mirrors hikes/walk/[uuid]/+server.js.
export async function GET({ fetch }) {
  const res = await fetch(`${API_BASE}/api/stars/history`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "stars history unavailable");
  }

  return json(await res.json(), {
    headers: cloudflareCacheHeaders(STARS_HISTORY_CACHE_CONTROL),
  });
}
