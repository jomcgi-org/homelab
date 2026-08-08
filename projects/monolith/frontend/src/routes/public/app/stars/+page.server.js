import { error } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. stars sits at the same
// depth as ships/hikes (routes/public/app/stars), hence four ../ segments to
// reach src/lib/. Mirrors hikes/+page.server.js.
import {
  STARS_SITES_CACHE_CONTROL,
  versionedEtag,
} from "../../../../lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";
const RETRY_DELAYS_MS = [100, 300];

function retryableStatus(status) {
  return [500, 502, 503, 504].includes(status);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchStars(fetch) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      const res = await fetch(`${API_BASE}/api/stars/sites`, {
        signal: AbortSignal.timeout(10_000),
      });
      if (!retryableStatus(res.status) || attempt >= RETRY_DELAYS_MS.length) {
        return res;
      }
    } catch (err) {
      if (attempt >= RETRY_DELAYS_MS.length) throw err;
    }
    await delay(RETRY_DELAYS_MS[attempt]);
  }
}

// SSR-only: the browser never calls /api/stars/* directly (that surface is not
// exposed publicly). This load runs server-side in the same pod and the CDN
// fans the result out to viewers. Live updates come from re-running this load
// on a 30 min timer (invalidateAll) in the page, not from a client-side poll
// (the refresh job runs 3-hourly).
export async function load({ fetch, setHeaders }) {
  const res = await fetchStars(fetch);
  if (!res.ok) {
    throw error(503, "stars sites unavailable");
  }

  const headers = { "cache-control": STARS_SITES_CACHE_CONTROL };
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  const lastModified = res.headers?.get?.("last-modified");
  if (lastModified) headers["last-modified"] = lastModified;
  setHeaders(headers);

  return { snapshot: await res.json() };
}
