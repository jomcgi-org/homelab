// imgproxy URL helpers for the /app/trips pages. Originals live in the
// monolith-trips SeaweedFS bucket; imgproxy resizes them on the fly and serves
// from img.jomcgi.dev (absolute, cross-origin <img>, no CORS needed). The
// presets mirror the sizes the old trips frontend used. `key` is the TripPoint
// `image` value (the S3 object key for that photo).
const IMG_BASE = "https://img.jomcgi.dev";

const PRESETS = {
  thumb: "rs:fit:300:300/q:85",
  display: "rs:fit:1920:1080/q:92",
  preview: "rs:fit:1200:1200/q:90",
  gallery: "rs:fit:600:600/q:88",
};

export const imgUrl = (key, preset) =>
  `${IMG_BASE}/unsafe/${PRESETS[preset]}/plain/s3://monolith-trips/${key}`;

export const fullUrl = (key) =>
  `${IMG_BASE}/unsafe/plain/s3://monolith-trips/${key}`;
