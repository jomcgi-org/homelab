import { error, json } from "@sveltejs/kit";
import { STARS_SITES_CACHE_CONTROL } from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the map-only snapshot. The refresh job publishes this
// separately from the detail payload so the map can start with coordinates and
// headline scores instead of parsing every forecast window.
export async function GET({ fetch }) {
  const res = await fetch(`${API_BASE}/api/stars/map`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw error(503, "stars map unavailable");
  return json(await res.json(), {
    headers: { "cache-control": STARS_SITES_CACHE_CONTROL },
  });
}
