// The public Turnstile site key gates inserts on the demo (see design doc
// the ember public-pages design). It is PUBLIC by design
// (it identifies the widget, not a credential); the Turnstile *secret* never
// enters the frontend, only the FastAPI backend. Read from the environment,
// same convention as routes/public/chat/+page.server.js and app/notes.
const TURNSTILE_SITE_KEY = process.env.TURNSTILE_SITE_KEY || "";

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

// Seed the initial paint with a cached status/savings read so the hero stat
// and state chip aren't blank before the client's first poll lands. Both
// reads are backend-cached control-plane reads (status: 500ms single-flight;
// savings: 30s), so this SSR fetch cannot wake the demo VM. Fail-soft: either
// missing just leaves the console to populate itself from its own onMount poll.
export async function load({ fetch }) {
  let status = null;
  let savings = null;
  try {
    const res = await fetch(`${API_BASE}/api/ember/postgres/status`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (res.ok) status = await res.json();
  } catch {
    // leave status null; the console's own poll fills it in
  }
  try {
    const res = await fetch(`${API_BASE}/api/ember/postgres/savings`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (res.ok) savings = await res.json();
  } catch {
    // leave savings null; the console's own poll fills it in
  }

  return {
    turnstileSiteKey: TURNSTILE_SITE_KEY,
    initialStatus: status,
    initialSavings: savings,
  };
}
