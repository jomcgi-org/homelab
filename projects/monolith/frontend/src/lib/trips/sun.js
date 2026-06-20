// Minimal solar-position + sunset math, inlined rather than pulling in the
// `suncalc` npm package (a new dep + lockfile churn for two functions is not
// worth it). Ported from the public-domain SunCalc core
// (https://github.com/mourner/suncalc, BSD-2-Clause) and trimmed to just the
// two things the day-view telemetry needs: the sun's altitude at a moment, and
// the day's sunset time. Pure and dependency free, so it is unit testable.
//
// Astronomy refs are the same as SunCalc: Astronomy Answers position formulae
// (aa.quae.nl/en/reken/zonpositie.html).

const PI = Math.PI;
const rad = PI / 180;
const dayMs = 1000 * 60 * 60 * 24;
const J1970 = 2440588;
const J2000 = 2451545;
const e = rad * 23.4397; // obliquity of the Earth

function toDays(date) {
  return date.valueOf() / dayMs - 0.5 + J1970 - J2000;
}

function rightAscension(l, b) {
  return Math.atan2(
    Math.sin(l) * Math.cos(e) - Math.tan(b) * Math.sin(e),
    Math.cos(l),
  );
}

function declination(l, b) {
  return Math.asin(
    Math.sin(b) * Math.cos(e) + Math.cos(b) * Math.sin(e) * Math.sin(l),
  );
}

function altitudeRad(H, phi, dec) {
  return Math.asin(
    Math.sin(phi) * Math.sin(dec) + Math.cos(phi) * Math.cos(dec) * Math.cos(H),
  );
}

function azimuthRad(H, phi, dec) {
  return Math.atan2(
    Math.sin(H),
    Math.cos(H) * Math.sin(phi) - Math.tan(dec) * Math.cos(phi),
  );
}

function siderealTime(d, lw) {
  return rad * (280.16 + 360.9856235 * d) - lw;
}

function solarMeanAnomaly(d) {
  return rad * (357.5291 + 0.98560028 * d);
}

function eclipticLongitude(M) {
  const C =
    rad *
    (1.9148 * Math.sin(M) + 0.02 * Math.sin(2 * M) + 0.0003 * Math.sin(3 * M)); // equation of center
  const P = rad * 102.9372; // perihelion of the Earth
  return M + C + P + PI;
}

function sunCoords(d) {
  const M = solarMeanAnomaly(d);
  const L = eclipticLongitude(M);
  return { dec: declination(L, 0), ra: rightAscension(L, 0) };
}

// Sun altitude (radians, negative below the horizon) at `date` for the given
// latitude / longitude in degrees. Matches SunCalc.getPosition().altitude.
export function sunAltitude(date, lat, lng) {
  const lw = rad * -lng;
  const phi = rad * lat;
  const d = toDays(date);
  const c = sunCoords(d);
  const H = siderealTime(d, lw) - c.ra;
  return altitudeRad(H, phi, c.dec);
}

// Sun azimuth (radians, measured from south to west: 0 = south, +PI/2 = west) at
// `date` for the given latitude / longitude in degrees. Matches
// SunCalc.getPosition().azimuth, which the day-map hillshade uses to relight the
// terrain from the sun's bearing at the photo's capture time.
export function sunAzimuth(date, lat, lng) {
  const lw = rad * -lng;
  const phi = rad * lat;
  const d = toDays(date);
  const c = sunCoords(d);
  const H = siderealTime(d, lw) - c.ra;
  return azimuthRad(H, phi, c.dec);
}

// --- sunset time ---
const J0 = 0.0009;

function julianCycle(d, lw) {
  return Math.round(d - J0 - lw / (2 * PI));
}
function approxTransit(Ht, lw, n) {
  return J0 + (Ht + lw) / (2 * PI) + n;
}
function solarTransitJ(ds, M, L) {
  return J2000 + ds + 0.0053 * Math.sin(M) - 0.0069 * Math.sin(2 * L);
}
function hourAngle(h, phi, d) {
  return Math.acos(
    (Math.sin(h) - Math.sin(phi) * Math.sin(d)) / (Math.cos(phi) * Math.cos(d)),
  );
}
function fromJulian(j) {
  return new Date((j + 0.5 - J1970) * dayMs);
}

// Sunset Date for the day containing `date` at the given location, or null when
// the sun does not set (polar day / night: the hour-angle acos goes out of
// domain and yields NaN). Matches SunCalc.getTimes().sunset (altitude -0.833°).
export function sunsetTime(date, lat, lng) {
  const lw = rad * -lng;
  const phi = rad * lat;
  const d = toDays(date);
  const n = julianCycle(d, lw);
  const ds = approxTransit(0, lw, n);
  const M = solarMeanAnomaly(ds);
  const L = eclipticLongitude(M);
  const dec = declination(L, 0);

  const h0 = -0.833 * rad; // standard sunset altitude
  const w = hourAngle(h0, phi, dec);
  if (Number.isNaN(w)) return null;
  const a = approxTransit(w, lw, n);
  const Jset = solarTransitJ(a, M, L);
  const set = fromJulian(Jset);
  return Number.isNaN(set.getTime()) ? null : set;
}
