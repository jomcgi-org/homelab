/** Deterministic SSR default. Callers must pass the same locale on both sides. */
export const DEFAULT_LOCALE = "en-US";

/** Availability describes a measurement, never its health or success. */
export const MEASUREMENT_STATES = Object.freeze({
  AVAILABLE: "available",
  UNAVAILABLE: "unavailable",
});

/**
 * Format only finite numbers. Units are caller-owned literal text, not an Intl
 * unit identifier. Significant digits let compact rounding promote 999,950 to
 * 1M, and preserve fractional measurements instead of rounding them to zero.
 * Exact text preserves the number's significant digits, not the compact result.
 * Unsupported locale tags fall back to en-US, never the host's ambient locale.
 * The default export condition exposes this helper in Node without a compiler;
 * the svelte condition additionally exposes components from index.js.
 */
export function formatMeasurement(
  value,
  { unit = "", locale = DEFAULT_LOCALE } = {},
) {
  if (typeof unit !== "string") throw new TypeError("unit must be text");
  if (typeof locale !== "string" || !locale.trim()) {
    throw new TypeError("locale must be a non-empty locale tag");
  }
  locale = Intl.NumberFormat.supportedLocalesOf(locale).length
    ? locale
    : DEFAULT_LOCALE;
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return Object.freeze({
      state: MEASUREMENT_STATES.UNAVAILABLE,
      text: "Unavailable",
      exactText: "Unavailable",
      unit,
      locale,
    });
  }
  const compact = new Intl.NumberFormat(locale, {
    notation: "compact",
    maximumSignificantDigits: 3,
  });
  const exact = new Intl.NumberFormat(locale, {
    maximumSignificantDigits: 21,
  });
  return Object.freeze({
    state: MEASUREMENT_STATES.AVAILABLE,
    text: compact.format(value),
    exactText: [exact.format(value), unit].filter(Boolean).join(" "),
    unit,
    locale,
  });
}
