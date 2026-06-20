// Shared preset name list for the /app/trips imgproxy images. Originals live in
// the monolith-trips SeaweedFS bucket; imgproxy resizes them on the fly and
// serves them same-origin under /img (the monolith-public HTTPRoute forwards
// /img/ to imgproxy, which runs with IMGPROXY_PATH_PREFIX=/img). URL building +
// HMAC signing happens server-side in $lib/server/trips-img.js (the signing
// secret must never reach the client); this module only holds the preset names
// so the server signer and any caller share one source of truth.
//
// imgproxy is locked to these named presets (IMGPROXY_ONLY_PRESETS); the names
// MUST match IMGPROXY_PRESETS in the monolith-public chart (chart/values.yaml).
export const PRESETS = new Set([
  "thumb",
  "gallery",
  "preview",
  "display",
  "full",
]);
