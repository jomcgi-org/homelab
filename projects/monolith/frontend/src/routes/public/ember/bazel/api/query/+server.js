// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the bazel skyframe query roundtrip (see
// ember_public/bazel_router.py POST /query). The backend's own read timeout
// on the workload submit is 25s; give the proxy a slightly wider ceiling so
// the backend's own 504 is what a visitor sees on a slow clone, not a proxy
// timeout racing it. The request cookie header is forwarded upstream
// unmodified so the backend can read the bazel_query_session cookie (session
// attribution, rate limiting); there is no set-cookie to relay back since
// this endpoint never mints a session itself.
export async function POST({ request, fetch }) {
  const headers = { "content-type": "application/json" };
  const cookie = request.headers.get("cookie");
  if (cookie) headers.cookie = cookie;

  const res = await fetch(`${API_BASE}/api/ember/bazel/query`, {
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
