import { json } from "@sveltejs/kit";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Same-origin proxy for the all-time "estimated cold analysis time skipped"
// counter (see ember_public/bazel_router.py GET /savings). Reads the
// monolith's own Postgres, never the bazel-query workload, so this can never
// consume a clone. No cookie forwarding: session-optional, mirrors the
// postgres demo's savings proxy.
export async function GET({ fetch }) {
  const res = await fetch(`${API_BASE}/api/ember/bazel/savings`, {
    signal: AbortSignal.timeout(5_000),
  });
  const body = await res.json();
  return json(body, { status: res.status });
}
