import { error } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. This file sits one level
// deeper than ships ([slug] under routes/public/app/trips), hence five ../
// segments to reach src/lib/.
import {
  TRIPS_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";

// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// fallback would silently route to the wrong backend if the env var were missing
// (semgrep sveltekit-server-hardcoded-api-base-fallback).
const API_BASE = process.env.API_BASE;

// One SSR fetch of {trip, points} shared by the summary, timeline and per-day
// pages (they all read it from the merged layout data, so the trip is loaded
// once per navigation into a trip, not per child route). SSR-only: the browser
// never touches /api/trips/*.
export async function load({ params, fetch, setHeaders }) {
  const res = await fetch(
    `${API_BASE}/api/trips/trip/${encodeURIComponent(params.slug)}`,
    { signal: AbortSignal.timeout(10_000) },
  );
  if (res.status === 404) {
    throw error(404, "trip not found");
  }
  if (!res.ok) {
    throw error(503, "trip unavailable");
  }

  const headers = { "cache-control": TRIPS_CACHE_CONTROL };
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  const { trip, points } = await res.json();
  return { trip, points };
}
