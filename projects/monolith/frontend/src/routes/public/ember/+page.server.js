// /ember landing: one SSR read of the demo's cached status + savings so the
// Postgres card can show a live state word and the running savings counter.
// Both are backend-cached control-plane reads (status: 500ms single-flight;
// savings: 30s), so this fetch cannot wake the demo VM. Fail-soft: a missing
// read just renders the card without its live line.

// nosemgrep: sveltekit-server-hardcoded-api-base-fallback
const API_BASE = process.env.API_BASE || "http://localhost:8000";

export async function load({ fetch }) {
  let status = null;
  let savings = null;
  try {
    const res = await fetch(`${API_BASE}/api/ember/postgres/status`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (res.ok) status = await res.json();
  } catch {
    // leave status null; the card degrades to static copy
  }
  try {
    const res = await fetch(`${API_BASE}/api/ember/postgres/savings`, {
      signal: AbortSignal.timeout(5_000),
    });
    if (res.ok) savings = await res.json();
  } catch {
    // leave savings null; the card omits the counter
  }

  return { status, savings };
}
