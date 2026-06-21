import { error } from "@sveltejs/kit";

// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// fallback would silently route to the wrong backend if the env var were missing
// (semgrep sveltekit-server-hardcoded-api-base-fallback). Mirrors trips/ships.
const API_BASE = process.env.API_BASE;

// SSR-only: the browser never calls /api/wc2026/* directly. This load runs
// server-side in the same pod and the CDN fans the result out to viewers. The
// summary is recomputed by a periodic sim, so the page barely changes between
// loads; a 5 minute edge cache keeps it cheap.
export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/wc2026/summary`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "wc2026 data unavailable");
  }

  setHeaders({ "cache-control": "public, max-age=300" });

  return await res.json();
}
