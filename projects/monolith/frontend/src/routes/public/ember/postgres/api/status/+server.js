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
//
// The `p` query param is the console's ephemeral per-page client id, forwarded
// so the backend can count live watchers (the "N here now" pill). It is not the
// insert session cookie (which this proxy deliberately never forwards); it only
// exists to make the shared VM's warmth legible.
export async function GET({ fetch, url }) {
  const target = new URL(`${API_BASE}/api/ember/postgres/status`);
  const clientId = url.searchParams.get("p");
  if (clientId) target.searchParams.set("p", clientId);
  const res = await fetch(target, {
    signal: AbortSignal.timeout(5_000),
  });
  const body = await res.json();
  return json(body, {
    status: res.status,
    headers: { "cache-control": "no-store" },
  });
}
