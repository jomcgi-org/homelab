import { json } from "@sveltejs/kit";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the all-time "scan time saved" counter (see
// ember_public/semgrep_router.py GET /savings). Reads the monolith's own
// Postgres, never the fc-invoke workload, so this can never queue a scan.
// No cookie forwarding: session-optional.
export async function GET({ fetch }) {
  const res = await fetch(`${API_BASE}/api/ember/semgrep/savings`, {
    signal: AbortSignal.timeout(5_000),
  });
  const body = await res.json();
  return json(body, { status: res.status });
}
