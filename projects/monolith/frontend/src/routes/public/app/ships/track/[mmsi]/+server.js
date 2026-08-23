import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  SHIPS_TRACK_CACHE_CONTROL,
} from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for one vessel's track history. The map calls this site
// route (/app/ships/track/{mmsi}) on marker click; the fetch to /api/ships/*
// happens server-side here, keeping that private surface off the browser.
export async function GET({ params, fetch }) {
  const res = await fetch(
    `${API_BASE}/api/ships/track/${encodeURIComponent(params.mmsi)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(503, "ship track unavailable");
  }

  return json(await res.json(), {
    headers: cloudflareCacheHeaders(SHIPS_TRACK_CACHE_CONTROL),
  });
}
