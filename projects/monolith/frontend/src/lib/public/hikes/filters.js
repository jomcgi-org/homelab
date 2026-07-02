// Pure, unit-tested filter helpers for the /app/hikes planner, originally ported
// from the now-removed standalone hikes frontend. Semantics
// are kept identical: characteristic bounds are inclusive, the radius test uses
// the same haversine, and date bucketing uses the UK (Europe/London) calendar
// day, since the walks are Scotland-based.
//
// Window tuples are the compact serving format: [ts, temp_c, precip_mm,
// wind_kmh, cloud_pct], where ts is a unix timestamp in seconds (the
// /api/hikes/walks endpoint serializes them this way).

const WINDOW_TS = 0;
const WINDOW_TEMP = 1;
const WINDOW_PRECIP = 2;
const WINDOW_WIND = 3;
const WINDOW_CLOUD = 4;

// Haversine great-circle distance in km. Identical to calculateDistance in the
// old app.js.
export function calculateDistance(lat1, lon1, lat2, lon2) {
  const R = 6371; // Earth's radius in km
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLon = ((lon2 - lon1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLon / 2) *
      Math.sin(dLon / 2);
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  return R * c;
}

// Filter by numeric characteristics. Bounds are inclusive: a walk exactly at a
// limit passes (matches the old `< min || > max` / `> max` ladder). A bound left
// undefined / null / NaN is treated as "no constraint" so a sidebar that only
// sets some of the five does not silently drop everything.
export function filterWalksByCharacteristics(walks, filters = {}) {
  const {
    minDuration = -Infinity,
    maxDuration = Infinity,
    minDistance = -Infinity,
    maxDistance = Infinity,
    maxAscent = Infinity,
  } = filters;

  const lo = (v, fallback) => (v == null || Number.isNaN(v) ? fallback : v);
  const minDur = lo(minDuration, -Infinity);
  const maxDur = lo(maxDuration, Infinity);
  const minDist = lo(minDistance, -Infinity);
  const maxDist = lo(maxDistance, Infinity);
  const maxAsc = lo(maxAscent, Infinity);

  return walks.filter((walk) => {
    if (walk.duration_h < minDur || walk.duration_h > maxDur) return false;
    if (walk.distance_km < minDist || walk.distance_km > maxDist) return false;
    if (walk.ascent_m > maxAsc) return false;
    return true;
  });
}

// Walks within radiusKm of (lat, lon), annotated with distance_from_user (km)
// and sorted nearest-first. Reads latitude/longitude (the API field names),
// falling back to the legacy lat/lng shape so the helper works on either.
export function filterWalksByLocation(walks, lat, lon, radiusKm) {
  return walks
    .map((walk) => {
      const wLat = walk.latitude ?? walk.lat;
      const wLon = walk.longitude ?? walk.lng;
      const distance = calculateDistance(lat, lon, wLat, wLon);
      return { ...walk, distance_from_user: distance };
    })
    .filter((walk) => walk.distance_from_user <= radiusKm)
    .sort((a, b) => a.distance_from_user - b.distance_from_user);
}

// UK (Europe/London) calendar day string (YYYY-MM-DD) for a Date. Mirrors the
// old code's day bucketing, which always used Europe/London because the walks
// are in Scotland.
export function ukDayString(date) {
  // en-CA renders ISO-style YYYY-MM-DD, so we get the London-local calendar day
  // without manual padding.
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Europe/London",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

// Group a walk's window tuples by their UK-local calendar day. Returns an object
// keyed by YYYY-MM-DD whose values are the (chronologically sorted) tuples for
// that day. Past windows are dropped relative to `now` so the strip only shows
// usable time, matching the old filterWindowsByWeather "exclude windows that
// have already started" rule.
export function groupWindowsByDay(walk, now = new Date()) {
  const nowSec = now.getTime() / 1000;
  const windows = (walk?.windows ?? [])
    .filter((w) => w[WINDOW_TS] >= nowSec)
    .sort((a, b) => a[WINDOW_TS] - b[WINDOW_TS]);

  const byDay = {};
  for (const w of windows) {
    const day = ukDayString(new Date(w[WINDOW_TS] * 1000));
    (byDay[day] ??= []).push(w);
  }
  return byDay;
}

// The UK-local calendar days for the next `n` days starting today (inclusive),
// as YYYY-MM-DD strings. n=1 is just today.
//
// We anchor on the UK-local calendar day of `now` and walk the day component
// forward (parsing the YYYY-MM-DD key at noon UTC so the increment stays inside
// the right calendar day across BST/GMT), rather than adding a fixed 24h in ms.
// Adding 86_400_000 ms skips or duplicates a calendar day across the spring/
// autumn DST transition; incrementing the day component yields N consecutive UK
// calendar days with no skip or duplicate. Mirrors the legacy app.js
// generateDateOptions, which did date.setDate(ukToday.getDate() + i).
export function upcomingUkDays(n, now = new Date()) {
  // Noon UTC on the UK-local "today" sits comfortably inside that calendar day
  // for both BST (UTC+1) and GMT, so day-component arithmetic never slips into
  // a neighbouring day.
  const anchor = new Date(`${ukDayString(now)}T12:00:00Z`);
  const days = [];
  for (let i = 0; i < n; i++) {
    const d = new Date(anchor);
    d.setUTCDate(anchor.getUTCDate() + i);
    days.push(ukDayString(d));
  }
  return days;
}

// True if the walk has at least one viable window whose UK-local day falls in
// the next `n` days (today through today + n - 1). n=1 means "viable today".
// Past windows do not count.
export function viableInNextDays(walk, n, now = new Date()) {
  const days = new Set(upcomingUkDays(n, now));
  const byDay = groupWindowsByDay(walk, now);
  for (const day of Object.keys(byDay)) {
    if (days.has(day) && byDay[day].length > 0) return true;
  }
  return false;
}

// Convenience accessors for a window tuple, used by the map card so it does not
// reach into raw indices.
export function windowFields(w) {
  return {
    ts: w[WINDOW_TS],
    date: new Date(w[WINDOW_TS] * 1000),
    temp_c: w[WINDOW_TEMP],
    precip_mm: w[WINDOW_PRECIP],
    wind_kmh: w[WINDOW_WIND],
    cloud_pct: w[WINDOW_CLOUD],
  };
}
