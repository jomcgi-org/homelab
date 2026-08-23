import { error } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. campsites sits at the same
// depth as ships (routes/public/app/campsites), hence four ../ segments to
// reach src/lib/. Mirrors hikes/+page.server.js.
import {
  CAMPSITES_SNAPSHOT_CACHE_CONTROL,
  cloudflareCacheHeaders,
  versionedEtag,
} from "../../../../lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000"; // nosemgrep: sveltekit-server-hardcoded-api-base-fallback

// SSR-only: the browser never calls /api/campsites/* directly (that surface is
// not exposed publicly). This load runs server-side in the same pod and the CDN
// fans the result out to viewers. Live updates come from re-running this load
// on a 15 min timer (invalidateAll) in the page.
export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/campsites/snapshot`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "campsites data unavailable");
  }

  const headers = cloudflareCacheHeaders(CAMPSITES_SNAPSHOT_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  const lastModified = res.headers?.get?.("last-modified");
  if (lastModified) headers["last-modified"] = lastModified;
  setHeaders(headers);

  return { snapshot: await res.json() };
}
