import { json } from "@sveltejs/kit";

// The localhost fallback is the established convention across every public
// proxy (ships/stars/notes/body); prod sets API_BASE via values.yaml.
// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the demo-postgres lifecycle poll (see ember_public/router.py
// GET /status). This is a control-plane management read, not a connection to the
// demo workload itself, so the frontend can poll it sub-second without waking the
// VM. No cookie forwarding: status is session-optional. No caching: the whole point
// is watching state change in near real time.
export async function GET({ fetch }) {
  const res = await fetch(`${API_BASE}/api/ember/postgres/status`, {
    signal: AbortSignal.timeout(5_000),
  });
  const body = await res.json();
  return json(body, {
    status: res.status,
    headers: { "cache-control": "no-store" },
  });
}
