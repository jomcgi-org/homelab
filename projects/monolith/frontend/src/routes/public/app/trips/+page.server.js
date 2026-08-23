import { error } from "@sveltejs/kit";
// Relative (not $lib): vitest loads this module directly and its plain node
// config does not resolve the SvelteKit $lib alias. trips sits at the same depth
// as ships (routes/public/app/trips), hence four ../ segments to reach src/lib/.
// Mirrors ships/+page.server.js.
import {
  cloudflareCacheHeaders,
  TRIPS_CACHE_CONTROL,
  versionedEtag,
} from "../../../../lib/cache-headers.js";
// Server-only imgproxy URL signer (relative import: vitest loads this module
// directly without the $lib alias). See [slug]/+layout.server.js for the same
// pattern; the HMAC secret never leaves the server.
import { signedImgUrl } from "../../../../lib/server/trips-img.js";

// No URL fallback: API_BASE is injected via values.yaml in prod; a localhost
// fallback would silently route to the wrong backend if the env var were missing
// (semgrep sveltekit-server-hardcoded-api-base-fallback). Mirrors how the read
// API base is wired for the other public apps.
const API_BASE = process.env.API_BASE;

// SSR-only: the browser never calls /api/trips/* directly (that surface is not
// exposed publicly). This load runs server-side in the same pod and the CDN fans
// the result out to viewers. Trip content barely changes between loads.
export async function load({ fetch, setHeaders }) {
  const res = await fetch(`${API_BASE}/api/trips/trips`, {
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) {
    throw error(503, "trips index unavailable");
  }

  const headers = cloudflareCacheHeaders(TRIPS_CACHE_CONTROL);
  const etag = versionedEtag(res.headers?.get?.("etag"));
  if (etag) headers.etag = etag;
  setHeaders(headers);

  // Pre-sign each trip cover server-side (gallery preset) into `coverUrl` so the
  // index cards render a signed /img URL without ever touching the secret.
  const index = await res.json();
  const trips = (index.trips ?? []).map((t) =>
    t.default_image
      ? { ...t, coverUrl: signedImgUrl(t.default_image, "gallery") }
      : t,
  );
  return { index: { ...index, trips } };
}
