// The public Turnstile site key gates scans on the demo (see design doc
// the ember semgrep demo design). It is PUBLIC by design
// (it identifies the widget, not a credential); the Turnstile *secret* never
// enters the frontend, only the FastAPI backend. Read from the environment,
// same convention as routes/public/ember/postgres/+page.server.js.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Seed the initial paint with a cached savings read so the footer counter
// isn't blank before the client's own fetch lands. This is a backend-cached
// control-plane read (30s TTL), so the SSR fetch cannot trigger a scan or
// touch the fc-invoke workload. Fail-soft: a missing/failed read just leaves
// the page to populate itself from its own onMount fetch.
export async function load({ fetch }) {
  let savings = null;
  try {
    const res = await fetch(`${API_BASE}/api/ember/semgrep/savings`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (res.ok) savings = await res.json();
  } catch {
    // leave savings null; the page's own fetch fills it in
  }

  return {
    turnstileSiteKey: TURNSTILE_SITE_KEY,
    initialSavings: savings,
  };
}
