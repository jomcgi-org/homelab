// Pure helpers for the /app/stars heatmap layer and the historical month
// picker. Kept out of StarsMap.svelte so they stay unit-testable without a DOM
// or MapLibre (mirrors ships/deadReckoning.js, hikes/filters.js).

// Full month-of-year names, indexed 1..12 (index 0 is a pad so month numbers
// read directly). The historical layer buckets by month-of-year (ADR 008), so
// the picker and header label by name, not by a year-month.
export const MONTH_LABELS = [
  "",
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

// Short three-letter chip labels (Jan..Dec), indexed 1..12 to match.
export const MONTH_SHORT = [
  "",
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

// Full month name for a 1..12 number, or "" for anything out of range.
export function monthLabel(month) {
  return MONTH_LABELS[month] ?? "";
}

// Short month name for a 1..12 number, or "" for anything out of range.
export function monthShort(month) {
  return MONTH_SHORT[month] ?? "";
}

// Geometry for the per-site 12-month clear-dark-hours bar chart. Given the
// `months` map from the /history payload ({1..12: clear_dark_hours}, keys may be
// numbers or strings since JSON object keys stringify), returns a fixed array of
// 12 bars in month order. Each bar carries its raw `value`, its `frac` (value /
// the tallest month, 0..1, the bar height fraction), and `isMax` (the tallest
// month(s), so the card can label the notable bar). Pure + layout-agnostic: it
// emits numbers, not SVG, so it stays unit-testable without a DOM (mirrors how
// relativeMax keeps the normalization math out of StarsMap).
export function monthBars(months) {
  const values = [];
  for (let m = 1; m <= 12; m++) {
    const raw = Number(months?.[m] ?? months?.[String(m)] ?? 0);
    values.push(Number.isFinite(raw) && raw > 0 ? raw : 0);
  }
  const max = Math.max(0, ...values);
  return values.map((value, i) => ({
    month: i + 1,
    short: MONTH_SHORT[i + 1],
    value,
    frac: max > 0 ? value / max : 0,
    isMax: max > 0 && value === max,
  }));
}

// The relative normalization floor: the largest heat value across the current
// features, but never below `floor` (default 1) so an all-zero or single-point
// set still yields a strictly ascending interpolate domain (MapLibre rejects a
// stop list whose inputs are not increasing, which 0..0 would be). The heatmap
// rescales to whatever data is in view (live score vs historical clear-dark-hour
// counts, which differ per month/all-year), so switching mode/month re-derives
// this.
export function relativeMax(values, floor = 1) {
  let max = floor;
  for (const v of values) {
    if (typeof v === "number" && Number.isFinite(v) && v > max) max = v;
  }
  return max;
}

// MapLibre `heatmap-weight` expression that maps each feature's `heat` property
// linearly from 0..max onto 0..1, so the densest point reaches full weight
// regardless of the field's absolute scale (live score 0..100 vs historical
// clear-dark-hour counts, which vary widely per month). Recomputed and
// re-applied whenever the data changes.
export function heatWeightExpression(max) {
  return ["interpolate", ["linear"], ["get", "heat"], 0, 0, max, 1];
}
