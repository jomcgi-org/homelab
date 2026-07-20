// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the timed scan roundtrip (see ember_public/semgrep_router.py
// POST /scan). A warm scan is ~1s but the queue can add real wait time under
// load, so the timeout is generous (30s) rather than the 5s used by the
// savings poll. The request cookie header is forwarded upstream unmodified so
// the backend can read the demo_sg_session cookie (session gating, scan rate
// bucket); there is no set-cookie to relay back since this endpoint never
// mints a session itself. Status and body are passed through verbatim: the
// backend's 401/422/429/503/502 shapes are meant to reach the client as-is.
export async function POST({ request, fetch }) {
  const headers = { "content-type": "application/json" };
  const cookie = request.headers.get("cookie");
  if (cookie) headers.cookie = cookie;

  const res = await fetch(`${API_BASE}/api/ember/semgrep/scan`, {
    method: "POST",
    headers,
    body: await request.text(),
    signal: AbortSignal.timeout(30_000),
  });

  return new Response(await res.text(), {
    status: res.status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
    },
  });
}
