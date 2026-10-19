// Invented public fixtures. The clock is fixed by the tests, never production.
export const now = "2026-10-19T12:00:00Z";
export const sites = Array.from({ length: 9 }, (_, index) => ({
  id: index + 1,
  name: `Fixture site ${index + 1}`,
  lat: 56.35 + Math.floor(index / 3) * 0.036,
  lon: -4.7 + (index % 3) * 0.067,
  clear_dark_hours: 8 - (index % 3),
  best_hours: Array.from({ length: 8 - (index % 3) }, (_, hour) => ({
    time: `2026-10-${19 + Math.floor(hour / 4)}T${20 + (hour % 4)}:00:00Z`,
    cloud_area_fraction: 3 + (hour % 4),
    dark: true,
  })),
}));
export const data = {
  hasBlog: true,
  snapshot: {
    sites,
    count: sites.length,
    fetched_at: now,
    darkness: "astronomical",
  },
};
export const history = {
  sites: sites.map((site) => ({
    id: site.id,
    name: site.name,
    lat: site.lat,
    lon: site.lon,
    clear: [120, 94, 78, 48, 12, 0, 0, 34, 65, 110, 130, 140].map((v) =>
      Math.round((v * site.id) / sites.length),
    ),
    dark: [300, 260, 220, 100, 20, 10, 10, 70, 160, 220, 280, 310],
  })),
};
