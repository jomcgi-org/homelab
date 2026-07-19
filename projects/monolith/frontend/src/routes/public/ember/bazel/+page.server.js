// The public Turnstile site key gates the query session on this demo (see
// ADR embervm/010 and ember_public/bazel_router.py). It is PUBLIC by design
// (it identifies the widget, not a credential); the Turnstile *secret* never
// enters the frontend, only the FastAPI backend. Same convention as
// routes/public/ember/postgres/+page.server.js.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function load({ fetch }) {
  // No status endpoint for this demo (see plan Task 7): each query is a
  // one-shot task-class Assign, there is no VM lifecycle to poll.
  //
  // Seed the initial paint with a cached savings read so the counter isn't
  // blank before the client's own fetch lands, mirroring the postgres page's
  // SSR seed. The read is a backend-cached (30s) control-plane read, so this
  // SSR fetch is cheap. Fail-soft: a miss just leaves the counter to
  // populate itself from the client-side fetch on mount.
  let savings = null;
  try {
    const res = await fetch(`${API_BASE}/api/ember/bazel/savings`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (res.ok) savings = await res.json();
  } catch {
    // leave savings null; the page's own onMount fetch fills it in
  }

  return {
    turnstileSiteKey: TURNSTILE_SITE_KEY,
    initialSavings: savings,
  };
}
