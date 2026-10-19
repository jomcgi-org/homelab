// Invented parks and forecasts. Shared by DOM tests and the real-map preview.
export const dates = Array.from({ length: 14 }, (_, i) =>
  new Date(Date.UTC(2026, 9, 23 + i)).toISOString().slice(0, 10),
);
function park(id, name, lon, lat, scores) {
  return {
    id,
    name,
    lon,
    lat,
    region: "Synthetic BC region",
    booking_url: "https://example.com/booking",
    days: dates.map((date, i) => ({
      date,
      available: scores[i] != null,
      sunny_score: scores[i] ?? 0,
      is_good: (scores[i] ?? 0) >= 60,
      cloud: 100 - (scores[i] ?? 0),
      precip: i % 2 ? 2.4 : 0,
      temp_max: 16 - i,
    })),
  };
}
export const snapshot = {
  generated_at: "2026-10-19T12:00:00Z",
  parks: [
    park("alpha", "Alpha Lake", -123.8, 49.8, [90, 80, 60]),
    park("birch", "Birch Lake", -122.1, 50.9, [97, 30]),
    park("cedar", "Cedar Lake", -125.2, 51.8, [null, null, 20]),
    park("dormant", "Dormant Lake", -123, 53, []),
  ],
};
