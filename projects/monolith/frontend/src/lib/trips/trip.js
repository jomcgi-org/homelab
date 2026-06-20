// Pure trip-data helpers shared by the /app/trips pages. The backend serves raw
// points (GPS + capture time + optional image); the per-day grouping, distance
// and elevation stats used to live in the old React frontend and are ported here
// so the SvelteKit pages can derive them from the SSR payload. Kept dependency
// free so it is unit testable.

// Vibrant per-day line/accent colours. Not the restrained site tokens: a route
// map needs ~12 visually distinct hues, so this is local data colour (same
// rationale as ShipsMap's VESSEL_COLORS), not a new design system.
export const DAY_COLORS = [
  "#2563eb",
  "#059669",
  "#d97706",
  "#dc2626",
  "#7c3aed",
  "#0891b2",
  "#ea580c",
  "#db2777",
  "#16a34a",
  "#0d9488",
  "#9333ea",
  "#0284c7",
];

export const dayColor = (i) => DAY_COLORS[i % DAY_COLORS.length];

const NOISE_THRESHOLD = 5; // metres: GPS elevation noise floor near sea level
const CHANGE_THRESHOLD = 5; // metres: ignore sub-5m wiggle in ascent/descent

// "YYYY-MM-DD" for an ISO timestamp in the trip's IANA zone, so day grouping is
// stable regardless of where the SSR render runs (the server clock is UTC).
export function dayKey(iso, tz) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  try {
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: tz || "UTC",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(d);
  } catch {
    return d.toISOString().slice(0, 10);
  }
}

function haversineKm(a, b) {
  if (a?.lat == null || b?.lat == null) return 0;
  const R = 6371;
  const dLat = ((b.lat - a.lat) * Math.PI) / 180;
  const dLon = ((b.lng - a.lng) * Math.PI) / 180;
  const s =
    Math.sin(dLat / 2) ** 2 +
    Math.cos((a.lat * Math.PI) / 180) *
      Math.cos((b.lat * Math.PI) / 180) *
      Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(s), Math.sqrt(1 - s));
}

export function routeDistanceKm(points) {
  if (!points || points.length < 2) return 0;
  let total = 0;
  for (let i = 0; i < points.length - 1; i++) {
    total += haversineKm(points[i], points[i + 1]);
  }
  return Math.round(total);
}

// Ascent/descent over a point sequence, with the sea-level noise floor applied
// and sub-threshold changes ignored. Ported from the old deriveStats().
function ascentDescent(points) {
  const raw = points.map((p) => p.elevation).filter((e) => e != null);
  const hasElevation = raw.length > 0;
  const valid = raw.filter((e) => e > NOISE_THRESHOLD);
  const floor = valid.length ? Math.min(...valid) : 0;
  const clamp = (e) => (e <= NOISE_THRESHOLD ? floor : e);

  let ascent = 0;
  let descent = 0;
  if (hasElevation) {
    for (let i = 1; i < points.length; i++) {
      const prev = points[i - 1].elevation;
      const curr = points[i].elevation;
      if (prev == null || curr == null) continue;
      const diff = clamp(curr) - clamp(prev);
      if (Math.abs(diff) <= CHANGE_THRESHOLD) continue;
      if (diff > 0) ascent += diff;
      else descent += Math.abs(diff);
    }
  }
  const clamped = raw.map(clamp);
  return {
    hasElevation,
    ascent: Math.round(ascent),
    descent: Math.round(descent),
    maxElevation: clamped.length ? Math.round(Math.max(...clamped)) : null,
    minElevation: clamped.length ? Math.round(Math.min(...clamped)) : null,
  };
}

// Group every point (including image-less "gap" points that draw the line) into
// ordered days. dayNumber is 1-indexed by chronological day.
export function groupByDay(points, tz) {
  const buckets = new Map();
  for (const p of points || []) {
    const key = dayKey(p.taken_at, tz);
    if (!key) continue;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(p);
  }
  return [...buckets.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, pts], i) => ({
      dayNumber: i + 1,
      date,
      points: pts,
      distance: routeDistanceKm(pts),
      ...ascentDescent(pts),
    }));
}

// Whole-trip stats plus the per-day breakdown, for the summary page.
export function deriveTripStats(points, tz, configStats = {}) {
  if (!points?.length) return null;
  const days = groupByDay(points, tz);
  const lats = points.map((p) => p.lat).filter((v) => v != null);
  const lngs = points.map((p) => p.lng).filter((v) => v != null);
  const whole = ascentDescent(points);
  const distances = days.map((d) => d.distance);

  return {
    totalDistance: routeDistanceKm(points),
    totalDays: days.length,
    totalPoints: points.length,
    maxLat: lats.length ? Math.max(...lats) : null,
    minLat: lats.length ? Math.min(...lats) : null,
    startIso: points[0].taken_at,
    endIso: points[points.length - 1].taken_at,
    days,
    longestDay: distances.length ? Math.max(...distances) : 0,
    hasElevation: whole.hasElevation,
    maxElevation: whole.maxElevation,
    minElevation: whole.minElevation,
    totalAscent: whole.ascent,
    totalDescent: whole.descent,
    coldestTemp: configStats?.coldest_temp ?? null,
  };
}

// Day label from the trips.days JSON blob (keyed by stringified day number),
// falling back to "Day N".
export function dayLabel(tripDays, dayNumber) {
  const entry = tripDays?.[String(dayNumber)] || tripDays?.[dayNumber];
  return entry?.label || `Day ${dayNumber}`;
}

// Photos (image-bearing points) for one day, in capture order.
export function dayPhotos(dayPoints) {
  return (dayPoints || []).filter((p) => p.image);
}

// Clamp a scrubber index into [0, count-1] (returns 0 when there are no items).
// Pure so the day-page scrubber's prev/next + keyboard stepping is unit-testable
// without a Svelte render harness.
export function clampIndex(i, count) {
  if (!count || count < 1) return 0;
  return Math.max(0, Math.min(count - 1, i));
}

// Sampled elevation series for a sparkline (caps point count).
export function elevationSeries(points, maxPoints = 60) {
  const elevs = (points || []).map((p) => p.elevation).filter((e) => e != null);
  if (elevs.length < 2) return [];
  const step = Math.max(1, Math.floor(elevs.length / maxPoints));
  return elevs.filter((_, i) => i % step === 0);
}
