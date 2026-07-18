import { json } from "@sveltejs/kit";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the all-time "memory saved while asleep" counter (see
// ember_public/router.py GET /savings). Reads the monolith's own Postgres, never
// the demo VM, so this can never wake it. No cookie forwarding: session-optional.
export async function GET({ fetch }) {
  const res = await fetch(`${API_BASE}/api/ember/postgres/savings`, {
    signal: AbortSignal.timeout(5_000),
  });
  const body = await res.json();
  return json(body, { status: res.status });
}
