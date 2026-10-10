/** Stable meanings: callers supply a visible domain label alongside the cue. */
export const STATUS_KINDS = Object.freeze({
  ok: Object.freeze({
    label: "OK",
    meaning: "Healthy or successful",
    role: "--ds-ok",
    cue: "✓",
  }),
  warn: Object.freeze({
    label: "Warning",
    meaning: "Attention required",
    role: "--ds-warn",
    cue: "△",
  }),
  err: Object.freeze({
    label: "Error",
    meaning: "Failure",
    role: "--ds-err",
    cue: "×",
  }),
  unknown: Object.freeze({
    label: "Unknown",
    meaning: "Not known or unavailable",
    role: "--ds-ink-muted",
    cue: "?",
  }),
  pending: Object.freeze({
    label: "Pending",
    meaning: "Waiting or loading",
    role: "--ds-ink-muted",
    cue: "◷",
  }),
});

/** Ordered technical-drawing memory tiers. Markers preserve meaning without hue. */
export const SERIES_ROLES = Object.freeze([
  Object.freeze({
    id: "gpu",
    label: "GPU",
    role: "--ds-series-1",
    marker: "circle",
  }),
  Object.freeze({
    id: "host-ram",
    label: "Host RAM",
    role: "--ds-series-2",
    marker: "square",
  }),
  Object.freeze({
    id: "page-cache",
    label: "Page cache",
    role: "--ds-series-3",
    marker: "triangle",
  }),
  Object.freeze({
    id: "nvme",
    label: "NVMe",
    role: "--ds-series-4",
    marker: "diamond",
  }),
  Object.freeze({
    id: "hot-expert-set",
    label: "Hot expert set",
    role: "--ds-series-5",
    marker: "cross",
  }),
]);

export const CONTENT_STATES = Object.freeze({
  ready: "Ready",
  loading: "Loading",
  empty: "Empty",
  error: "Error",
  unavailable: "Unavailable",
});

export function requireText(value, name) {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`${name} must be non-empty text`);
  }
  return value;
}

export function requireKind(value, contract, name) {
  if (!Object.hasOwn(contract, value))
    throw new RangeError(`Unknown ${name}: ${value}`);
  return value;
}
