/** Deterministic SSR default. Callers must pass the same locale on both sides. */
export const DEFAULT_LOCALE = "en-US";

/** Availability describes a measurement, never its health or success. */
export const MEASUREMENT_STATES = Object.freeze({
  AVAILABLE: "available",
  UNAVAILABLE: "unavailable",
});

/**
 * Format only finite numbers. Units are caller-owned literal text, not an Intl
 * unit identifier. Compact suffixes use three significant digits, so 999,950
 * still promotes to 1M. Values with no compact suffix in the locale keep the
 * standard locale format (full integer digits, at most three fraction digits)
 * instead of significant-digit rounding, so de-DE shows -12.345 rather than
 * -12.300 and ja-JP shows 1,234 rather than 1230. Magnitudes that the standard
 * format would round to zero keep the compact significant-digit text, so a
 * nonzero measurement never renders as "0" or "-0". Exact text preserves the number's
 * significant digits, not the compact result. Unsupported locale tags fall back
 * to en-US, never the host's ambient locale. Plain Node without a Svelte
 * compiler imports this helper from the data-display/core subpath; the
 * data-display subpath always exposes the components from index.js.
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
  const standard = new Intl.NumberFormat(locale, { maximumFractionDigits: 3 });
  const exact = new Intl.NumberFormat(locale, {
    maximumSignificantDigits: 21,
  });
  const hasSuffix = compact
    .formatToParts(value)
    .some((part) => part.type === "compact");
  let text = hasSuffix ? compact.format(value) : standard.format(value);
  if (
    value !== 0 &&
    (text === standard.format(0) || text === standard.format(-0))
  ) {
    // The standard format rounds this magnitude to zero ("0" or "-0"); keep
    // the compact significant-digit text so a nonzero measurement never reads
    // as zero.
    text = compact.format(value);
  }
  return Object.freeze({
    state: MEASUREMENT_STATES.AVAILABLE,
    text,
    exactText: [exact.format(value), unit].filter(Boolean).join(" "),
    unit,
    locale,
  });
}
