import { error } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. This file sits one level
// deeper than ships ([slug] under routes/public/app/trips), hence five ../
// segments to reach src/lib/.
import {
  cloudflareCacheHeaders,
  TRIPS_CACHE_CONTROL,
  versionedEtag,
} from "../../../../../lib/cache-headers.js";
// Server-only: signs imgproxy URLs with the HMAC secret. Relative (not $lib) so
// vitest, which loads this module directly without the SvelteKit $lib alias, can
// resolve it (the signer's $env/dynamic/private import is aliased in
// vitest.config.js). $lib/server/** is never client-bundled, so the secret stays
// server-side; we pre-sign here and hand the components finished URLs.
import { signedImgUrl } from "../../../../../lib/server/trips-img.js";

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

  const headers = cloudflareCacheHeaders(TRIPS_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  const { trip, points } = await res.json();

  // Pre-sign every image URL server-side and attach it to the data. The day
  // scrubber needs the `display` preset, the grids/timeline need `gallery`, so
  // each image-bearing point carries both. Cover + highlight thumbs use
  // `gallery`. Fields are only added when the source image exists, so trips /
  // points without photos pass through unchanged.
  const signedPoints = (points ?? []).map((p) =>
    p.image
      ? {
          ...p,
          imgDisplay: signedImgUrl(p.image, "display"),
          imgGallery: signedImgUrl(p.image, "gallery"),
        }
      : p,
  );

  let signedTrip = trip;
  if (trip) {
    signedTrip = { ...trip };
    if (trip.default_image) {
      signedTrip.coverUrl = signedImgUrl(trip.default_image, "gallery");
    }
    if (Array.isArray(trip.highlights)) {
      signedTrip.highlights = trip.highlights.map((h) =>
        h.image ? { ...h, imgGallery: signedImgUrl(h.image, "gallery") } : h,
      );
    }
  }

  return { trip: signedTrip, points: signedPoints };
}
