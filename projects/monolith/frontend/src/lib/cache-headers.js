const ONE_HOUR = 3_600;
const ONE_DAY = 86_400;
const ONE_YEAR = 31_536_000;

// 60s fresh · 24h SWR (background refresh) · 1y SIE (cluster-down resilience)
export const PAGE_CACHE_CONTROL = `public, s-maxage=60, stale-while-revalidate=${ONE_DAY}, stale-if-error=${ONE_YEAR}`;

// /notes graph: gardener mutates on a schedule, so 1h freshness is fine. Mirrors
// _GRAPH_CACHE_CONTROL in projects/monolith/knowledge/router.py — keep in sync.
export const NOTES_PAGE_CACHE_CONTROL = `public, s-maxage=${ONE_HOUR}, stale-while-revalidate=${ONE_DAY}, stale-if-error=${ONE_YEAR}`;

// /app/ships snapshot: AIS positions refresh every ~2 min, so 120s freshness
// with a 10 min SWR window keeps the CDN serving warm data between ingests.
// Mirrors _SNAPSHOT_CACHE_CONTROL in projects/monolith/ships/router.py, keep in sync.
export const SHIPS_SNAPSHOT_CACHE_CONTROL =
  "public, s-maxage=120, stale-while-revalidate=600, stale-if-error=86400";

// /app/ships track: one vessel's history, fetched on marker click. Mirrors
// _TRACK_CACHE_CONTROL in projects/monolith/ships/router.py, keep in sync.
export const SHIPS_TRACK_CACHE_CONTROL =
  "public, s-maxage=60, stale-while-revalidate=300, stale-if-error=86400";

// /app/ships heat: traffic-density grid, rolled up hourly so 5 min fresh / 1 h
// SWR. Mirrors _HEAT_CACHE_CONTROL in projects/monolith/ships/router.py, keep in sync.
export const SHIPS_HEAT_CACHE_CONTROL =
  "public, s-maxage=300, stale-while-revalidate=3600, stale-if-error=86400";

// /app/hikes walks: forecasts refresh 6-hourly, so 30 min fresh with a 1 h SWR
// window is plenty. Mirrors _WALKS_CACHE_CONTROL in projects/monolith/hikes/router.py,
// keep in sync.
export const HIKES_WALKS_CACHE_CONTROL =
  "public, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400";
