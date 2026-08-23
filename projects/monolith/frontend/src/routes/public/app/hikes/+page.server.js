import { error } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. hikes sits at the same
// depth as ships (routes/public/app/hikes), hence four ../ segments to reach
// src/lib/. Mirrors ships/+page.server.js.
import {
  cloudflareCacheHeaders,
  HIKES_WALKS_CACHE_CONTROL,
  versionedEtag,
} from "../../../../lib/cache-headers.js";

const API_BASE = process.env.API_BASE || "http://localhost:8000";

// SSR-only: the browser never calls /api/hikes/* directly (that surface is not
// exposed publicly). This load runs server-side in the same pod and the CDN
// fans the result out to viewers. Live updates come from re-running this load
// on a 30 min timer (invalidateAll) in the page, not from a client-side poll
// (forecast windows only change 6-hourly).
export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/hikes/walks`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "hikes walks unavailable");
  }

  const headers = cloudflareCacheHeaders(HIKES_WALKS_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  const lastModified = res.headers?.get?.("last-modified");
  if (lastModified) headers["last-modified"] = lastModified;
  setHeaders(headers);

  return { snapshot: await res.json() };
}
