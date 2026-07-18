// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the timed demo-postgres roundtrip (see ember_public/router.py
// POST /query). A cold or relighting VM can take tens of seconds to answer, so the
// timeout is generous (90s) rather than the 5s used by the status/savings polls.
// The request cookie header is forwarded upstream unmodified so the backend can read
// the demo_pg_session cookie (session attribution, insert rate limiting); there is no
// set-cookie to relay back since this endpoint never mints a session itself.
export async function POST({ request, fetch }) {
  const headers = { "content-type": "application/json" };
  const cookie = request.headers.get("cookie");
  if (cookie) headers.cookie = cookie;

  const res = await fetch(`${API_BASE}/api/ember/postgres/query`, {
    method: "POST",
    headers,
    body: await request.text(),
    signal: AbortSignal.timeout(90_000),
  });

  return new Response(await res.text(), {
    status: res.status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
    },
  });
}
