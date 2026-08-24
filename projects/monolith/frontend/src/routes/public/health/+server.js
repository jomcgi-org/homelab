import { json } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly via server.test.js and
// its plain node config does not resolve the SvelteKit $lib alias. Mirrors the
// dr-jobs +page.server.js note. From routes/public/health/ that is three ../ to
// reach src/lib/.
import { HEALTH_CACHE_CONTROL } from "../../../lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// External health probe for the public tier (jomcgi.dev/health). Machine-readable
// JSON, not a page. The gateway routes only to this frontend, so this same-origin
// route is the only externally reachable health surface; it proxies server-side to
// the backend's deep check (read replica reachable + public_reader can query),
// keeping the /api surface off the browser. Success is edge-cached for 60s
// (HEALTH_CACHE_CONTROL) to cap origin load at ~1 req/min; the 503 path sets no
// cache header so failures are never cached and an outage surfaces within a cycle.
//
// The backend distinguishes two kinds of health component: fatal (which cause
// 503) and advisory (which are reported as degraded on a 200 response). This
// proxy must preserve that distinction: the frontend never lists advisory
// component names under failing, and it exposes degraded on both 200 and 503
// paths so the advisory signal is visible and correctly labelled.
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
    // Expose component names for attribution (fatal ones only), never internal
    // detail strings. Advisory components are listed separately under degraded.
    let failing = [];
    let degraded = [];
    try {
      const body = await res.json();
      // Guard the shape once: a non-array degraded (an older backend, a
      // mangled body) would otherwise become a Set of its characters and
      // start matching single-letter component names.
      degraded = Array.isArray(body?.degraded) ? body.degraded : [];
      const advisoryNames = new Set(degraded);
      failing = Object.entries(body?.components ?? {})
        .filter(([name, c]) => !c?.ok && !advisoryNames.has(name))
        .map(([name]) => name)
        .sort();
    } catch {
      // Non-JSON or unreadable error body: names are simply unavailable.
    }
    return json(
      {
        status: "unhealthy",
        backendStatus: res.status,
        ...(failing.length ? { failing } : {}),
        ...(degraded.length ? { degraded } : {}),
      },
      { status: 503 },
    );
  }

  const body = await res.json();
  const degraded = body.degraded ?? [];
  return json(
    {
      status: body.status,
      ...(degraded.length ? { degraded } : {}),
    },
    {
      headers: { "cache-control": HEALTH_CACHE_CONTROL },
    },
  );
}
