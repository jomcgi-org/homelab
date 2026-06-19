// imgproxy URL helpers for the /app/trips pages. Originals live in the
// monolith-trips SeaweedFS bucket; imgproxy resizes them on the fly and serves
// from img.jomcgi.dev (absolute, cross-origin <img>, no CORS needed). The
// presets mirror the sizes the old trips frontend used. `key` is the TripPoint
// `image` value (the S3 object key for that photo).
const IMG_BASE = "https://img.jomcgi.dev";

// imgproxy is locked to named presets (IMGPROXY_ONLY_PRESETS); these names must
// match IMGPROXY_PRESETS in the monolith-public chart.
const PRESETS = new Set(["thumb", "gallery", "preview", "display", "full"]);

export const imgUrl = (key, preset = "gallery") => {
  const p = PRESETS.has(preset) ? preset : "gallery";
  return `${IMG_BASE}/unsafe/${p}/plain/s3://monolith-trips/${key}`;
};

export const fullUrl = (key) =>
  `${IMG_BASE}/unsafe/full/plain/s3://monolith-trips/${key}`;
