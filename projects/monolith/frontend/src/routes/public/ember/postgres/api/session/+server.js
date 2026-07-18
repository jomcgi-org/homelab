// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for minting the demo-postgres session cookie (see
// ember_public/router.py POST /session). 90s timeout, matching /query: a fresh
// mint can race a cold-booting VM's status classification path even though the
// mint itself does not connect to the workload, so the generous ceiling keeps
// slow upstream conditions from surfacing as a proxy timeout instead of the
// backend's own in-band error shape.
//
// Cookie handling is bidirectional: the inbound request's cookie header is
// forwarded so the backend can no-op on an existing demo_pg_session cookie, and
// the upstream response's set-cookie header (the httpOnly cookie the backend
// mints) is relayed back verbatim. This is the one proxy in this family that
// needs raw set-cookie pass-through, since the cookie value is opaque and
// backend-chosen; there is nothing for this route to reissue itself.
export async function POST({ request, fetch }) {
  const headers = { "content-type": "application/json" };
  const cookie = request.headers.get("cookie");
  if (cookie) headers.cookie = cookie;

  const res = await fetch(`${API_BASE}/api/ember/postgres/session`, {
    method: "POST",
    headers,
    body: await request.text(),
    signal: AbortSignal.timeout(90_000),
  });

  const outHeaders = new Headers({
    "content-type": "application/json",
    "cache-control": "no-store",
  });
  const setCookie = res.headers.get("set-cookie");
  if (setCookie) outHeaders.set("set-cookie", setCookie);

  return new Response(await res.text(), {
    status: res.status,
    headers: outHeaders,
  });
}
