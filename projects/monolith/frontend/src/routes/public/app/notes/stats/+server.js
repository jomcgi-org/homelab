import { error, json } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. This endpoint sits at
// routes/public/app/notes/stats, so five ../ segments reach src/lib.
import {
  cloudflareCacheHeaders,
  STATS_CACHE_CONTROL,
} from "../../../../../lib/cache-headers.js";

// The localhost fallback is the established convention across every public
// proxy (ships/stars/notes/graph/body); prod sets API_BASE via values.yaml.
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the public cluster stats snapshot, the exact payload
// the homepage renders (home/observability/stats). The notes ticker SSR-seeds
// and then polls this for cluster and knowledge statistics. The browser never
// calls the backend directly (the gateway only routes to the frontend; mirrors the
// graph/body +server.js proxies).
//
// The backend serves a precomputed snapshot row (no GitHub/K8s call per
// request), so a short shared cache is plenty and shields it from poll storms.
export async function GET({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/home/observability/stats`, {
    signal: AbortSignal.timeout(8_000),
  });
  if (!res.ok) {
    throw error(503, "stats unavailable");
  }

  setHeaders(cloudflareCacheHeaders(STATS_CACHE_CONTROL));
  return json(await res.json());
}
