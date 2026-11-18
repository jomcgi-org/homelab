// Shared by the deploy-time HTML and browser. The forecast producer runs at
// "20 */2 * * *" (projects/monolith/chart/values.yaml). Allow three missed
// forecast cycles plus one site snapshot interval before marking it stale.
export const REFRESH_MS = 30 * 60_000;
export const SNAPSHOT_STALE_MS = 3 * REFRESH_MS;
export const FORECAST_REFRESH_MS = 2 * 60 * 60_000;
export const STALE_MS = 3 * FORECAST_REFRESH_MS + REFRESH_MS;

export function timestamp(value) {
  return typeof value === "string" && Number.isFinite(Date.parse(value))
    ? new Date(value).toISOString()
    : null;
}

export function isStale(fetchedAt, now) {
  const at = timestamp(fetchedAt);
  return at !== null && now - Date.parse(at) >= STALE_MS;
}

export function forecastStatus(fetchedAt, latestFetchedAt, now) {
  const at = timestamp(fetchedAt);
  if (!at) return "Forecast age unknown · retained weather windows";
  if (isStale(at, now)) return "Stale forecast · retained weather windows";
  const latest = timestamp(latestFetchedAt);
  if (latest && Date.parse(at) < Date.parse(latest))
    return "Earlier forecast retained · weather windows may be out of date";
  return "Forecast refreshes every 2 hours";
}

export function oldestForecast(timestamps) {
  const known = timestamps.map(timestamp).filter(Boolean).sort();
  return known[0] ?? null;
}

// A site refresh failure is distinct from retained upstream weather. Never
// use the R2 Last-Modified time to infer whether the 30-minute job ran.
export function snapshotStatus(generatedAt, now) {
  const at = timestamp(generatedAt);
  if (!at) return "Snapshot age unknown · published data retained";
  return now - Date.parse(at) >= SNAPSHOT_STALE_MS
    ? "Stale site snapshot · published data retained"
    : "Site refreshes every 30 minutes";
}
