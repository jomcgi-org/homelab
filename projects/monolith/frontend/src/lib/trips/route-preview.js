const MAX_DAYS = 32;
const MAX_POINTS_PER_DAY = 128;
const MERCATOR_LIMIT = 85.0511287798066;
const WIDTH = 640;
const HEIGHT = 320;
const PADDING = 32;

// The preview draws the same GPS route in Web Mercator, without a basemap.
// Unwrap adjacent longitudes before projecting so 179 to -179 crosses two
// degrees instead of drawing a line across the whole world.
export function routePreview(days = []) {
  const routes = [];
  let pointCount = 0;
  let dayCount = 0;
  let previousLon;
  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  let polar = false;
  for (const [index, day] of days.entries()) {
    const projected = [];
    let valid = 0;
    for (const point of day.points ?? []) {
      const { lat, lng } = point;
      if (
        !Number.isFinite(lat) ||
        !Number.isFinite(lng) ||
        Math.abs(lat) > 90 ||
        Math.abs(lng) > 180
      )
        continue;
      pointCount += 1;
      valid += 1;
      if (Math.abs(lat) > MERCATOR_LIMIT) {
        polar = true;
        continue;
      }
      const lon =
        previousLon === undefined
          ? lng
          : lng + 360 * Math.round((previousLon - lng) / 360);
      previousLon = lon;
      const x = (lon * Math.PI) / 180;
      const y = -Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
      projected.push([x, y]);
    }
    if (valid) dayCount += 1;
    if (projected.length)
      routes.push({ index, dayNumber: day.dayNumber ?? index + 1, projected });
  }
  const summary = { pointCount, dayCount, routes: [] };
  if (polar) return { ...summary, reason: "polar" };
  if (routes.length > MAX_DAYS || maxX - minX > Math.PI * 2) {
    return { ...summary, reason: "extent" };
  }
  if (!routes.length) return { ...summary, reason: "empty" };
  const spanX = maxX - minX;
  const spanY = maxY - minY;
  const scale = Math.min(
    spanX ? (WIDTH - 2 * PADDING) / spanX : Infinity,
    spanY ? (HEIGHT - 2 * PADDING) / spanY : Infinity,
  );
  const factor = Number.isFinite(scale) ? scale : 1;
  const offsetX = (WIDTH - spanX * factor) / 2;
  const offsetY = (HEIGHT - spanY * factor) / 2;
  return {
    ...summary,
    reason: null,
    routes: routes.map(({ index, dayNumber, projected }) => {
      const count = Math.min(projected.length, MAX_POINTS_PER_DAY);
      const samples = Array.from({ length: count }, (_, i) => {
        const position = Math.round(
          (i * (projected.length - 1)) / (count - 1 || 1),
        );
        const [x, y] = projected[position];
        return [
          Number(((x - minX) * factor + offsetX).toFixed(1)),
          Number(((y - minY) * factor + offsetY).toFixed(1)),
        ];
      });
      return {
        index,
        dayNumber,
        samples,
        points: samples.map((p) => p.join(",")).join(" "),
      };
    }),
  };
}
