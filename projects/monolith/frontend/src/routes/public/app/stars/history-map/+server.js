import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  STARS_HISTORY_CACHE_CONTROL,
} from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function GET({ fetch }) {
  const res = await fetch(`${API_BASE}/api/stars/history-map`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw error(503, "stars history map unavailable");
  return json(await res.json(), {
    headers: cloudflareCacheHeaders(STARS_HISTORY_CACHE_CONTROL),
  });
}
