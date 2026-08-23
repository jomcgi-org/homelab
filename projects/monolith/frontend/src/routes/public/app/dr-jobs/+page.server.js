import { error } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. dr-jobs sits at the same
// depth as hikes (routes/public/app/dr-jobs), hence four ../ segments to reach
// src/lib/. Mirrors hikes/+page.server.js.
import {
  cloudflareCacheHeaders,
  DR_JOBS_LISTINGS_CACHE_CONTROL,
  versionedEtag,
} from "../../../../lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// SSR-only: the browser never calls /api/dr-jobs/* directly (that surface is not
// exposed publicly). This load runs server-side in the same pod and the CDN
// fans the result out to viewers. Live updates come from re-running this load on
// a timer (invalidateAll) in the page; the scrape runs daily, so the table
// barely changes between loads.
export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/dr-jobs/listings`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "dr-jobs listings unavailable");
  }

  const headers = cloudflareCacheHeaders(DR_JOBS_LISTINGS_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  return { listings: await res.json() };
}
