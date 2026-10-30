// Invented walks. The clock is fixed by the tests, never production.
export const now = "2026-10-19T12:00:00Z";
export const days = ["2026-10-19", "2026-10-20", "2026-10-21"];
const regions = [
  ["Fort William", 56.83, -5.25],
  ["Cairngorms", 57.12, -3.63],
  ["Galloway", 55.02, -4.08],
];
export const walks = Array.from({ length: 9 }, (_, index) => {
  const [region, lat, lon] = regions[index % 3];
  return {
    uuid: `walk-${index + 1}`,
    name: `Fixture walk ${index + 1}`,
    region,
    latitude: lat + Math.floor(index / 3) * 0.05,
    longitude: lon + (index % 3) * 0.04,
    distance_km: 6 + index * 2,
    ascent_m: 200 + index * 120,
    duration_h: 2 + index * 0.75,
    url: `https://example.invalid/walks/${index + 1}`,
    // Longer walks fit fewer days; the last fits none.
    viable_days: index === 8 ? [] : days.slice(0, 3 - Math.floor(index / 4)),
  };
});
export const data = {
  hasBlog: true,
  snapshot: { count: walks.length, generated_at: now, walks },
};
// Window tuples: [unix seconds, temp C, rain mm, wind km/h, cloud %].
const at = (day, hour) => Date.parse(`${day}T${hour}:00:00Z`) / 1000;
export const detail = {
  summary: "An invented hill walk for the fixture.",
  windows: [
    [at("2026-10-19", "13"), 11, 0.0, 14, 20],
    [at("2026-10-19", "14"), 12, 0.1, 16, 30],
    [at("2026-10-20", "09"), 9, 0.0, 10, 10],
  ],
};
