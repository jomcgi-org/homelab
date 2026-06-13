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

// The relative normalization floor: the largest heat value across the current
// features, but never below `floor` (default 1) so an all-zero or single-point
// set still yields a strictly ascending interpolate domain (MapLibre rejects a
// stop list whose inputs are not increasing, which 0..0 would be). The heatmap
// rescales to whatever data is in view, so switching mode/month re-derives this.
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
// sum_q, which can be much larger). Recomputed and re-applied whenever the data
// changes.
export function heatWeightExpression(max) {
  return ["interpolate", ["linear"], ["get", "heat"], 0, 0, max, 1];
}
