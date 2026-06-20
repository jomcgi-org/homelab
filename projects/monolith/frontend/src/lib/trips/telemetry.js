// Per-photo telemetry for the day-view scrubber: position, elevation, cumulative
// distance, bearing, exposure and solar context for the currently selected
// photo. Ported from the old React DayDetailPage's DataPanel / currentPhoto
// computations so the SvelteKit page can derive the same readouts from the SSR
// payload. Pure and dependency free (solar math is inlined in ./sun.js) so it is
// unit testable without a Svelte render harness.

import { sunAltitude, sunsetTime } from "./sun.js";

function takenMs(p) {
  if (!p?.taken_at) return null;
  const t = new Date(p.taken_at).getTime();
  return Number.isNaN(t) ? null : t;
}

// Great-circle distance in km between two {lat,lng} points (haversine).
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

// Interpolate {lat, lng, elevation} for a photo from the day's GPS track by
// timestamp, used when the photo itself lacks a fix. Ported from the React
// `currentPhoto` useMemo: walk the (time-ordered) track, linearly interpolate
// between the points bracketing the photo's capture time, and clamp to the first
// / last point outside the track's span. Returns the photo's own values when it
// already has a fix or cannot be interpolated.
export function interpolatePosition(photo, dayPoints) {
  const out = {
    lat: photo?.lat ?? null,
    lng: photo?.lng ?? null,
    elevation: photo?.elevation ?? null,
  };
  if (photo?.lat != null && photo?.lng != null) return out;

  const photoTime = takenMs(photo);
  if (photoTime == null || !dayPoints?.length) return out;

  let prev = null;
  for (const point of dayPoints) {
    const pt = takenMs(point);
    if (pt == null) continue;
    if (pt >= photoTime) {
      if (prev) {
        const prevTime = takenMs(prev);
        const t = (photoTime - prevTime) / (pt - prevTime);
        return {
          lat: prev.lat + (point.lat - prev.lat) * t,
          lng: prev.lng + (point.lng - prev.lng) * t,
          elevation:
            prev.elevation != null && point.elevation != null
              ? prev.elevation + (point.elevation - prev.elevation) * t
              : (photo?.elevation ?? point.elevation ?? null),
        };
      }
      // Photo precedes the first track point: clamp to it.
      return {
        lat: point.lat,
        lng: point.lng,
        elevation: point.elevation ?? out.elevation,
      };
    }
    prev = point;
  }
  // Photo after the last track point: clamp to it.
  if (prev) {
    return {
      lat: prev.lat,
      lng: prev.lng,
      elevation: prev.elevation ?? out.elevation,
    };
  }
  return out;
}

// Compass bearing in degrees (0..360, 0 = north) of the track direction at the
// photo's capture time: the heading of the first track segment whose end is at
// or after the photo. Ported from the React `bearing` useMemo.
export function bearingAt(dayPoints, photoTime) {
  if (!dayPoints?.length || photoTime == null) return null;
  let prev = null;
  for (const point of dayPoints) {
    const pt = takenMs(point);
    if (pt != null && pt >= photoTime && prev) {
      const lat1 = (prev.lat * Math.PI) / 180;
      const lat2 = (point.lat * Math.PI) / 180;
      const dLng = ((point.lng - prev.lng) * Math.PI) / 180;
      const y = Math.sin(dLng) * Math.cos(lat2);
      const x =
        Math.cos(lat1) * Math.sin(lat2) -
        Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
      const brng = (Math.atan2(y, x) * 180) / Math.PI;
      return (brng + 360) % 360;
    }
    prev = point;
  }
  return null;
}

// 8-point compass arrow glyph for a bearing in degrees.
export function compassArrow(deg) {
  if (deg == null) return "→"; // →
  const arrows = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖"];
  return arrows[Math.round(deg / 45) % 8];
}

// Cumulative distance travelled into the day up to the photo's capture time, and
// the day's total distance, in km. Ported from the React `progress` useMemo.
export function cumulativeKm(dayPoints, photoTime) {
  if (!dayPoints?.length || photoTime == null) return null;
  let traveled = 0;
  let total = 0;
  let prev = null;
  let found = false;
  for (const point of dayPoints) {
    if (prev) {
      const seg = haversineKm(prev, point);
      total += seg;
      const pt = takenMs(point);
      if (!found && pt != null && pt >= photoTime) found = true;
      if (!found) traveled += seg;
    }
    prev = point;
  }
  return {
    km: Math.round(traveled),
    total: Math.round(total),
    percent: total > 0 ? Math.round((traveled / total) * 100) : 0,
  };
}

// Exposure-value mood label, from the EXIF light value (EV). Original thresholds.
export function evLabel(ev) {
  if (ev == null) return "";
  if (ev >= 13) return "BRIGHT";
  if (ev >= 10) return "SUNNY";
  if (ev >= 7) return "OVERCAST";
  if (ev >= 4) return "DIM";
  return "DARK";
}

// Sun-altitude mood label (degrees). Original thresholds.
export function solarLabel(altDeg) {
  if (altDeg == null) return "--";
  if (altDeg < -6) return "NIGHT";
  if (altDeg < 0) return "TWILIGHT";
  if (altDeg < 10) return "LOW";
  return "DAY";
}

// "Xh YYm" until sunset, or null when sunset is unknown / already past. Ported
// from the React getLightRemaining.
export function lightRemaining(photoTime, sunsetMs) {
  if (photoTime == null || sunsetMs == null || Number.isNaN(sunsetMs)) {
    return null;
  }
  if (photoTime > sunsetMs) return null;
  const diff = sunsetMs - photoTime;
  const hours = Math.floor(diff / (1000 * 60 * 60));
  const mins = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
  return `${hours}h ${mins.toString().padStart(2, "0")}m`;
}

// Format a latitude/longitude as e.g. "12.3456° N" / "12.3456° W".
export function formatCoord(val, isLat) {
  if (val == null) return "--";
  const dir = isLat ? (val >= 0 ? "N" : "S") : val >= 0 ? "E" : "W";
  return `${Math.abs(val).toFixed(4)}° ${dir}`;
}

// Wall-clock HH:MM + AM/PM in the trip's IANA zone, for the big TIME readout.
export function formatClock(iso, tz) {
  if (!iso) return { time: "--:--", period: "" };
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return { time: "--:--", period: "" };
  try {
    const s = d.toLocaleTimeString("en-US", {
      timeZone: tz || "UTC",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
    });
    const parts = s.match(/(\d+:\d+)\s*(AM|PM)/i);
    if (parts) return { time: parts[1], period: parts[2].toUpperCase() };
    return { time: s, period: "" };
  } catch {
    return { time: "--:--", period: "" };
  }
}

// Bundle every per-photo readout for the telemetry panel. `dayPoints` is the
// day's full GPS track (image-less gap points included), `tz` the trip zone.
// Position / elevation are interpolated from the track when the photo lacks a
// fix; solar fields use that interpolated location.
export function photoTelemetry(photo, dayPoints, tz) {
  if (!photo) return null;
  const pos = interpolatePosition(photo, dayPoints);
  const photoTime = takenMs(photo);
  const { time, period } = formatClock(photo.taken_at, tz);

  let solarAltDeg = null;
  let light = null;
  if (photoTime != null && pos.lat != null && pos.lng != null) {
    const date = new Date(photoTime);
    solarAltDeg = (sunAltitude(date, pos.lat, pos.lng) * 180) / Math.PI;
    const sunset = sunsetTime(date, pos.lat, pos.lng);
    light = lightRemaining(photoTime, sunset ? sunset.getTime() : null);
  }

  const bearing = bearingAt(dayPoints, photoTime);
  const dist = cumulativeKm(dayPoints, photoTime);

  return {
    lat: pos.lat,
    lng: pos.lng,
    elevation: pos.elevation,
    time,
    period,
    solarAltDeg,
    solarLabel: solarLabel(solarAltDeg),
    light,
    ev: photo.light_value ?? null,
    evLabel: evLabel(photo.light_value),
    bearing,
    bearingArrow: compassArrow(bearing),
    km: dist?.km ?? null,
    totalKm: dist?.total ?? null,
    // Optics passed straight through for the panel to format.
    focalLength35mm: photo.focal_length_35mm ?? null,
    aperture: photo.aperture ?? null,
    iso: photo.iso ?? null,
    shutterSpeed: photo.shutter_speed ?? null,
  };
}
