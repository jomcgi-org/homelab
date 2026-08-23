import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  SHIPS_HEAT_CACHE_CONTROL,
} from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the traffic-density heatmap grid. The map fetches this
// site route (/app/ships/heat) when the user toggles into Heat mode; the fetch
// to /api/ships/* happens server-side here, keeping that private surface off
// the browser. Mirrors track/[mmsi]/+server.js.
export async function GET({ fetch }) {
  const res = await fetch(`${API_BASE}/api/ships/heat`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "ship heat unavailable");
  }

  return json(await res.json(), {
    headers: cloudflareCacheHeaders(SHIPS_HEAT_CACHE_CONTROL),
  });
}
