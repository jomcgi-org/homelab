import { version } from "$app/environment";

const ONE_HOUR = 3_600;
const ONE_DAY = 86_400;
const ONE_YEAR = 31_536_000;

// Fold the SvelteKit build version into a page ETag. A page's rendered HTML is a
// function of (data x build): a layout-only deploy changes the HTML and its
// hashed asset references but not the underlying data, so a data-derived ETag
// stays fixed and both browsers and the CDN keep revalidating to a 304 against
// pre-deploy HTML (see projects/monolith/dr_jobs/router.py for the data-side
// ETag). Prefixing the build version busts the validator on any deploy that
// changes the build, while still 304-ing cheaply when neither data nor build
// moved. `version` is SvelteKit's per-build timestamp, so it changes iff the
// frontend output changed (Bazel caches the build action otherwise).
//
// Returns undefined when there is no upstream ETag, so callers keep their
// existing "set the header only if present" guard.
export function versionedEtag(dataEtag) {
  if (!dataEtag) return undefined;
  const inner = dataEtag.replace(/^"|"$/g, "");
  return `"${version}-${inner}"`;
}

// Cloudflare treats s-maxage as proxy-revalidate, so a response that combines
// s-maxage with stale-while-revalidate or stale-if-error cannot actually be
// served stale. Keep the browser policy in Cache-Control and give Cloudflare a
// higher-precedence, CDN-only policy where the shared TTL is expressed as
// max-age instead. The hostname Cache Rule still decides which responses are
// eligible for storage.
export function cloudflareCacheHeaders(cacheControl) {
  const directives = cacheControl.split(",").map((part) => part.trim());
  const sharedTtl = directives.find((part) => part.startsWith("s-maxage="));
  if (!sharedTtl) {
    throw new Error("Cloudflare cache policy requires s-maxage");
  }

  const cloudflareDirectives = directives
    .filter((part) => !part.startsWith("max-age="))
    .map((part) =>
      part.startsWith("s-maxage=")
        ? part.replace("s-maxage=", "max-age=")
        : part,
    );

  return {
    "cache-control": cacheControl,
    "cloudflare-cdn-cache-control": cloudflareDirectives.join(", "),
  };
}

// 60s fresh · 24h SWR (background refresh) · 1y SIE (cluster-down resilience)
export const PAGE_CACHE_CONTROL = `public, s-maxage=60, stale-while-revalidate=${ONE_DAY}, stale-if-error=${ONE_YEAR}`;

// /health probe: deliberately the inverse of the data caches above. A 60s edge
// cache caps origin load at ~1 req/min, but health MUST surface a real outage,
// so there is NO stale-if-error (it would serve a stale 200 while the origin is
// down) and NO stale-while-revalidate (it would mask a fresh transition to
// unhealthy for a cycle). Only the 200 is cached; the frontend leaves the header
// off the 503 path so failures are never cached. 60s detection lag is fine: this
// is a personal service with no SLA.
export const HEALTH_CACHE_CONTROL = "public, max-age=0, s-maxage=60";

// /notes graph: gardener mutates on a schedule, so 1h freshness is fine. Mirrors
// _GRAPH_CACHE_CONTROL in projects/monolith/knowledge/router.py — keep in sync.
export const NOTES_PAGE_CACHE_CONTROL = `public, s-maxage=${ONE_HOUR}, stale-while-revalidate=${ONE_DAY}, stale-if-error=${ONE_YEAR}`;

// /docs pages: the manifest is baked into the build, so doc content changes only
// on deploy. The build-versioned page ETag busts revalidation on every deploy,
// so a long edge cache is safe: 1h fresh, 1d background refresh, 1y serve-stale
// on origin error (cluster-down resilience). The browser revalidates each load
// and 304s cheaply when neither data nor build moved.
export const DOCS_CACHE_CONTROL = `public, s-maxage=${ONE_HOUR}, stale-while-revalidate=${ONE_DAY}, stale-if-error=${ONE_YEAR}`;

// These app data endpoints (SSR-fetched JSON) all carry `max-age=0` so the
// BROWSER revalidates every load instead of holding a stale copy. Without an
// explicit max-age, Cloudflare injects a zone Browser-Cache-TTL (~1 h), which
// made the live ships map show hour-stale vessels and stranded /app/hikes on a
// pre-deploy payload shape. The CDN still caches via `s-maxage`, and the ETag
// makes browser revalidation a cheap 304 when nothing changed.

// /app/ships snapshot: AIS positions refresh every ~2 min, so 120s edge
// freshness with a 10 min SWR window keeps the CDN serving warm data between
// ingests; max-age=0 keeps the browser from caching positions for an hour.
// Mirrors _SNAPSHOT_CACHE_CONTROL in projects/monolith/ships/router.py, keep in sync.
export const SHIPS_SNAPSHOT_CACHE_CONTROL =
  "public, max-age=0, s-maxage=120, stale-while-revalidate=600, stale-if-error=86400";

// /app/ships track: one vessel's history, fetched on marker click. Mirrors
// _TRACK_CACHE_CONTROL in projects/monolith/ships/router.py, keep in sync.
export const SHIPS_TRACK_CACHE_CONTROL =
  "public, max-age=0, s-maxage=60, stale-while-revalidate=300, stale-if-error=86400";

// /app/ships heat: traffic-density grid, rolled up hourly so 5 min fresh / 1 h
// SWR. Mirrors _HEAT_CACHE_CONTROL in projects/monolith/ships/router.py, keep in sync.
export const SHIPS_HEAT_CACHE_CONTROL =
  "public, max-age=0, s-maxage=300, stale-while-revalidate=3600, stale-if-error=86400";

// /app/hikes walks: forecasts refresh 6-hourly, so 30 min edge freshness with a
// 1 h SWR window is plenty. Mirrors _WALKS_CACHE_CONTROL in
// projects/monolith/hikes/router.py, keep in sync.
export const HIKES_WALKS_CACHE_CONTROL =
  "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400";

// /app/dr-jobs listings: the NHS scrape runs daily, so 30 min edge freshness
// with a 1 h SWR window is plenty. Mirrors _LISTINGS_CACHE_CONTROL in
// projects/monolith/dr_jobs/router.py, keep in sync.
export const DR_JOBS_LISTINGS_CACHE_CONTROL =
  "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400";

// /app/stars sites: the refresh job runs 3-hourly and elapsed hours are pruned
// hourly, so 30 min edge freshness with a 1 h SWR window is plenty. Mirrors
// _SITES_CACHE_CONTROL in projects/monolith/stars/router.py, keep in sync.
export const STARS_SITES_CACHE_CONTROL =
  "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400";

// /app/stars history (the bulk month layer and the per-site 12-month breakdown):
// immutable between reloads, the bytes change only when the ~yearly ERA5/CERRA
// climatology reload runs (ADR 009), whose runbook purges the Cloudflare cache
// for /app/stars/history* right after. So cache it at the edge for a year and
// invalidate explicitly. Mirrors _HISTORY_CACHE_CONTROL in
// projects/monolith/stars/router.py, keep in sync.
export const STARS_HISTORY_CACHE_CONTROL =
  "public, max-age=0, s-maxage=31536000, stale-while-revalidate=604800, stale-if-error=604800";

// /app/trips pages (the trip index + per-trip metadata and points). Trip content
// is edited in place (title, cover, the occasional backfill), so a short CDN TTL
// matters: a 24h edge cache made edits invisible for a day and required a manual
// Cloudflare purge. 5 min CDN with stale-while-revalidate keeps the SSR pages
// snappy (CDN offload) while edits propagate within minutes without a purge.
// Mirrors _CACHE in projects/monolith/trips/read_router.py, keep in sync.
export const TRIPS_CACHE_CONTROL =
  "public, max-age=60, s-maxage=300, stale-while-revalidate=3600";

// /app/campsites snapshot: availability + 14-day weather, refreshed hourly by
// the campsites-refresh CronWorkflow. 60s edge freshness so a deploy becomes
// visible within ~1 min without a CDN purge: on deploy the SvelteKit build
// version changes, which busts the versioned page ETag, and the short s-maxage
// means the CDN revalidates and picks up the new build within a minute instead of
// holding the pre-deploy render for up to 30 min. Browser max-age=0 already
// revalidates each load, and the (build x data)-derived ETag keeps the extra
// revalidations cheap 304s when nothing moved. 1 h SWR / 1 d SIE preserve CDN
// offload and origin-outage resilience. Mirrors _SNAPSHOT_CACHE_CONTROL in
// projects/monolith/campsites/router.py, keep in sync.
export const CAMPSITES_SNAPSHOT_CACHE_CONTROL =
  "public, max-age=0, s-maxage=60, stale-while-revalidate=3600, stale-if-error=86400";

// /app/grimoire read API (the api/[...path] catch-all JSON + binary image proxy
// and the book/read pagination proxy). The corpus is a read-only, near-static
// D&D book library, so it takes an aggressive 1 h edge cache: this is the CDN
// offload that keeps a share-driven traffic spike off the origin. max-age=0 keeps
// the browser revalidating so a redeploy (new coverage while extraction runs) is
// visible within the hour, while s-maxage lets Cloudflare fan warm copies out to
// viewers. 1 d SWR (background refresh) and 1 d SIE (serve-stale on origin error)
// preserve offload and resilience. Note the sibling book/read proxy signs
// image_key -> image_url per response, but the signature is deterministic for a
// given key so the signed page is safe to edge-cache.
export const GRIMOIRE_READ_CACHE_CONTROL = `public, max-age=0, s-maxage=${ONE_HOUR}, stale-while-revalidate=${ONE_DAY}, stale-if-error=${ONE_DAY}`;
