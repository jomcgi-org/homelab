import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

// A worker takes its CSP from its own response (HOME-419). Browsers cached the
// first worker response for a year (`/assets/*` is immutable) with the site
// policy, which blocks the tile host. A new query makes returning browsers
// fetch the corrected response. Raise the number when the worker's headers
// change again.
export const MAP_WORKER_URL = `${workerUrl}?csp=2`;
