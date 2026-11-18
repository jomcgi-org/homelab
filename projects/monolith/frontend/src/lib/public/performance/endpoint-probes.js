// These GET handlers do not consume or forward query parameters. Keep this
// allowlist narrow: a new route requires checking its loader/proxy first.
export const PROBE_ENDPOINTS = Object.freeze([
  "/app/hikes/__data.json",
  "/app/hikes/walk/7fc20756-89c2-5280-9be9-3ddb3db01e44",
  "/app/ships/__data.json",
  "/app/ships/track/369514002",
  "/app/trips/__data.json",
  "/app/ships/heat",
  "/app/notes/graph",
  "/app/notes/stats",
  "/scotland/marketplace.json",
]);

export function validateProbeEndpoints(endpoints, samples) {
  if (!Number.isInteger(samples) || samples < 1 || samples > 20)
    throw new Error("Cache-miss probes require 1 to 20 samples per endpoint");
  if (
    !endpoints.length ||
    endpoints.length > PROBE_ENDPOINTS.length ||
    new Set(endpoints).size !== endpoints.length ||
    endpoints.some((endpoint) => !PROBE_ENDPOINTS.includes(endpoint))
  )
    throw new Error(
      "Cache-miss probes require unique, allowlisted public endpoint paths",
    );
}

export function probeUrl(origin, endpoint, nonce, sample) {
  validateProbeEndpoints([endpoint], sample);
  const url = new URL(endpoint, origin);
  url.searchParams.set("__public_perf_probe", `${nonce}-${sample}`);
  return url.href;
}

export function endpointCacheGroups(samples, summarizeTimes) {
  const groups = {};
  for (const sample of samples) {
    const status = sample.headers?.["cf-cache-status"] ?? "unavailable";
    (groups[status] ??= []).push(sample);
  }
  return Object.fromEntries(
    Object.entries(groups).map(([status, rows]) => [
      status,
      { samples: rows.length, ...summarizeTimes(rows) },
    ]),
  );
}
