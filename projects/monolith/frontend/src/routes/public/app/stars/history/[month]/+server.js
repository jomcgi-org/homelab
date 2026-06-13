import { error, json } from "@sveltejs/kit";
import { STARS_HISTORY_CACHE_CONTROL } from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the historical heatmap layer (ADR 008). The page calls
// this site route (/app/stars/history/{month}) when it switches to Historical
// mode or the month changes; the fetch to /api/stars/history happens here,
// server-side in the same pod, keeping that private surface off the browser.
// Mirrors hikes/walk/[uuid]/+server.js. `month` is 1..12; the API clamps and
// defaults internally, so an out-of-range value still returns a (possibly
// empty) payload rather than 4xx.
export async function GET({ params, fetch }) {
  const month = Number(params.month);
  const res = await fetch(
    `${API_BASE}/api/stars/history?month=${encodeURIComponent(month)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(503, "stars history unavailable");
  }

  return json(await res.json(), {
    headers: { "cache-control": STARS_HISTORY_CACHE_CONTROL },
  });
}
