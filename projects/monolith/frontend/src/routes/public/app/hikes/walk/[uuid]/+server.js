import { error, json } from "@sveltejs/kit";
import {
  cloudflareCacheHeaders,
  HIKES_WALKS_CACHE_CONTROL,
} from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for one walk's detail (summary + hourly windows). The map
// calls this site route (/app/hikes/walk/{uuid}) on marker click; the fetch to
// /api/hikes/* happens server-side here, keeping that private surface off the
// browser. Mirrors ships/track/[mmsi]/+server.js. The light corpus list omits
// windows, so this is what fills the selected-walk card.
export async function GET({ params, fetch }) {
  const res = await fetch(
    `${API_BASE}/api/hikes/walks/${encodeURIComponent(params.uuid)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (!res.ok) {
    throw error(res.status === 404 ? 404 : 503, "hike detail unavailable");
  }

  return json(await res.json(), {
    headers: cloudflareCacheHeaders(HIKES_WALKS_CACHE_CONTROL),
  });
}
