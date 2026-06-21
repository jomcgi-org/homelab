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

// Geometry for the per-site 12-month clear-dark-hours bar chart. Accepts either
// a 12-element array (clear-dark hours, index 0 = January, the shape the
// /history payload now carries per site) or the legacy {1..12: clear_dark_hours}
// map (keys may be numbers or strings since JSON object keys stringify). Returns
// a fixed array of 12 bars in month order. Each bar carries its raw `value`, its
// `frac` (value / the tallest month, 0..1, the bar height fraction), and `isMax`
// (the tallest month(s), so the card can label the notable bar). Pure +
// layout-agnostic: it emits numbers, not SVG, so it stays unit-testable without
// a DOM (mirrors how relativeMax keeps the normalization math out of StarsMap).
export function monthBars(months) {
  const at = Array.isArray(months)
    ? (m) => months[m - 1]
    : (m) => months?.[m] ?? months?.[String(m)];
  const values = [];
  for (let m = 1; m <= 12; m++) {
    const raw = Number(at(m) ?? 0);
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

// Sum a 12-element month array, coercing non-finite entries to zero (an absent
// or short array reads as zero), so the all-year view never yields NaN.
function sumMonths(arr) {
  let total = 0;
  for (const v of arr ?? []) {
    if (typeof v === "number" && Number.isFinite(v)) total += v;
  }
  return total;
}

// Project one all-months history row onto the selected view. `site` is a
// {id, name, lat, lon, clear:[12], dark:[12]} row from the /history payload and
// `month` is 1..12, or 0 for the all-year sum. Returns the scalar headline shape
// the map field + detail card read (clear_dark_hours / dark_hours / clear_rate),
// keeping the per-site `clear` array so the card can still draw the 12-bar chart
// without a second request. Returns null when the site has no dark hours in the
// selected view, so the caller drops it (matching the old server-side per-view
// filter). Pure + unit-testable: this is the client-side replacement for the
// month filtering the API used to do per request.
export function projectHistory(site, month) {
  if (!site) return null;
  const clearArr = site.clear ?? [];
  const darkArr = site.dark ?? [];
  let clear;
  let dark;
  if (month === 0) {
    clear = sumMonths(clearArr);
    dark = sumMonths(darkArr);
  } else {
    clear = Number(clearArr[month - 1] ?? 0) || 0;
    dark = Number(darkArr[month - 1] ?? 0) || 0;
  }
  if (dark <= 0) return null;
  return {
    id: site.id,
    name: site.name,
    lat: site.lat,
    lon: site.lon,
    clear_dark_hours: clear,
    dark_hours: dark,
    clear_rate: dark ? clear / dark : 0,
    clear: clearArr,
  };
}

// The site rows the historical map plots for the selected month (1..12, or 0 for
// all year): every site projected onto that view, the zero-dark sites dropped,
// sorted by clear-dark hours descending so the richest spot leads (the API used
// to do this server-side per month; it now happens once in the browser over the
// single all-months payload).
export function historyView(sites, month) {
  const out = [];
  for (const site of sites ?? []) {
    const row = projectHistory(site, month);
    if (row) out.push(row);
  }
  out.sort((a, b) => b.clear_dark_hours - a.clear_dark_hours);
  return out;
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

const HOUR_MS = 3_600_000;

// A forecast hour is still "upcoming" until the end of its clock hour (its start
// + 1 h) is in the future. Mirrors the server's read-time prune, applied on the
// client so a long-open page drops hours that elapse between data refreshes.
// `iso` carries a UTC offset, so Date.parse yields an absolute instant: this is
// timezone-safe regardless of the viewer's locale (Vancouver or otherwise). An
// unparseable time is kept rather than silently dropped.
export function isUpcoming(iso, nowMs) {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? true : t + HOUR_MS > nowMs;
}

const NIGHT_SHIFT_MS = 12 * HOUR_MS;

// The viewing-night key (YYYY-MM-DD) an hour belongs to, matching the server's
// retired _night_key. A night runs from one evening into the next morning, so
// shifting the instant back 12 h and taking its UK-local date folds the evening
// and the following pre-dawn hours onto one key (one outing = one night).
export function nightKey(iso) {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) return null;
  return new Date(ms - NIGHT_SHIFT_MS).toLocaleDateString("en-CA", {
    timeZone: "Europe/London",
  });
}

// A site's upcoming clear windows (the live layer): best_hours with already-
// elapsed hours dropped. This is the clear-twilight superset the card lists; the
// map field filters it further by darkness mode (see the component). Shared by
// the map field, the night buckets and the card so a coloured cell always has
// card rows. Returns a new array, never mutates the input.
export function liveWindows(site, nowMs) {
  return (site?.best_hours ?? []).filter((h) => isUpcoming(h.time, nowMs));
}

// The sorted union of upcoming viewing-night keys across all sites, for the
// night-filter chips. Derived from the same windows the map colours by, so a
// fully-elapsed night drops out and a chip can never offer a night with no
// selectable windows. In astronomical mode only true-dark windows seed a night;
// in the midsummer twilight fallback every clear-twilight window does. Hours with
// no `dark` flag (older payload) count as dark, preserving prior behaviour.
export function starsNights(sites, darkness, nowMs) {
  const darkOnly = darkness !== "twilight";
  const keys = new Set();
  for (const site of sites ?? []) {
    for (const h of liveWindows(site, nowMs)) {
      if (darkOnly && h.dark === false) continue;
      const key = nightKey(h.time);
      if (key) keys.add(key);
    }
  }
  return [...keys].sort();
}
