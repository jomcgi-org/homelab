// Invented route and camera metadata. The swatches intentionally do not pretend
// to be photographs; production image URL selection is exercised unchanged.
const swatch = (label) =>
  `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="slategray"/><path d="M0 500L260 160L520 500L660 300L800 500" fill="darkslategray"/><text x="40" y="550" fill="white" font-size="28">Synthetic image ${label}</text></svg>`)}`;
export function tripsData() {
  const points = Array.from({ length: 6 }, (_, i) => ({
    id: `fixture-${i}`,
    lat: 49 + i * 0.04,
    lng: -123 + i * 0.05,
    elevation: 100 + i * 70,
    taken_at: `2026-10-${i < 3 ? "17" : "18"}T${16 + (i % 3)}:00:00Z`,
    image: `fixture-${i}.jpg`,
    imgGallery: swatch(`${i + 1}, gallery`),
    imgDisplay: swatch(`${i + 1}, display`),
    tags: i % 2 ? ["town"] : ["trail"],
    focal_length_35mm: 35,
    aperture: 4,
    iso: 200,
    shutter_speed: "1/250",
  }));
  const trip = {
    slug: "fixture-walk",
    title: "A journey through the mountains",
    short_title: "Mountain journey",
    subtitle: "Two days on the trail.",
    tz: "America/Vancouver",
    coverUrl: points[0].imgGallery,
    days: { 1: { label: "Forest to ridge" }, 2: { label: "Ridge to town" } },
    highlights: [
      {
        id: "ridge",
        day: 1,
        title: "The ridge",
        imgGallery: points[1].imgGallery,
      },
    ],
    stats: { coldest_temp: -2 },
  };
  return {
    trip,
    points,
    dayNumber: 1,
    index: { trips: [trip] },
    hasBlog: true,
  };
}
