// Extrapolate a vessel's position from its last fix using speed (knots) + course (deg).
export function deadReckon({ lat, lon, speed, course }, elapsedSeconds) {
  if (!speed || speed <= 0) return { lat, lon };
  const metersPerSec = speed * 0.514444;
  const dist = metersPerSec * elapsedSeconds;
  const R = 6371000;
  const brng = (course ?? 0) * (Math.PI / 180);
  const lat1 = (lat * Math.PI) / 180;
  const lon1 = (lon * Math.PI) / 180;
  const lat2 = Math.asin(
    Math.sin(lat1) * Math.cos(dist / R) +
      Math.cos(lat1) * Math.sin(dist / R) * Math.cos(brng),
  );
  const lon2 =
    lon1 +
    Math.atan2(
      Math.sin(brng) * Math.sin(dist / R) * Math.cos(lat1),
      Math.cos(dist / R) - Math.sin(lat1) * Math.sin(lat2),
    );
  return { lat: (lat2 * 180) / Math.PI, lon: (lon2 * 180) / Math.PI };
}
