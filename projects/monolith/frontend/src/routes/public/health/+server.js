import { json } from "@sveltejs/kit";
import { HEALTH_CACHE_CONTROL } from "$lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// External health probe for the public tier (jomcgi.dev/health). Machine-readable
// JSON, not a page. The gateway routes only to this frontend, so this same-origin
// route is the only externally reachable health surface; it proxies server-side to
// the backend's deep check (read replica reachable + public_reader can query),
// keeping the /api surface off the browser. Success is edge-cached for 60s
// (HEALTH_CACHE_CONTROL) to cap origin load at ~1 req/min; the 503 path sets no
// cache header so failures are never cached and an outage surfaces within a cycle.
export async function GET({ fetch }) {
  let res;
  try {
    res = await fetch(`${API_BASE}/api/health`, {
      signal: AbortSignal.timeout(5_000),
    });
  } catch {
    return json(
      { status: "unhealthy", reason: "backend unreachable" },
      { status: 503 },
    );
  }

  if (!res.ok) {
    return json(
      { status: "unhealthy", backendStatus: res.status },
      { status: 503 },
    );
  }

  return json(await res.json(), {
    headers: { "cache-control": HEALTH_CACHE_CONTROL },
  });
}
