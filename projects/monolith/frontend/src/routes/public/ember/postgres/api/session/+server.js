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
// mints) is relayed back with ONE rewrite. The backend scopes the cookie to its
// own mount path (Path=/api/ember/postgres), but the visitor's browser talks to
// this proxy family at /ember/postgres/api/*, so a verbatim relay stores a
// cookie the browser never sends back: inserts then fail session_required even
// after a solved Turnstile check. Rescope the Path to the public page's own
// prefix; the cookie value itself stays opaque and backend-chosen.
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
  if (setCookie) {
    outHeaders.set(
      "set-cookie",
      setCookie.replace(/Path=\/api\/ember\/postgres/i, "Path=/ember/postgres"),
    );
  }

  return new Response(await res.text(), {
    status: res.status,
    headers: outHeaders,
  });
}
