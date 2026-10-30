// Invented public AIS positions around the Solent. No real vessel identities.
const types = [60, 70, 80, 40, 50, null];
export const snapshot = {
  vessels: Array.from({ length: 48 }, (_, i) => ({
    mmsi: 900000001 + i,
    name: `Fixture vessel ${i + 1}`,
    ship_type: types[i % types.length],
    lat: 50.775 + (i % 8) * 0.0015,
    lon: -1.31 + Math.floor(i / 8) * 0.024 + (i % 3) * 0.003,
    speed: 4 + (i % 12),
    course: (i * 37) % 360,
    heading: (i * 37) % 360,
    destination: "Fixture harbour",
    recorded_at: "2026-10-19T12:00:00Z",
  })),
};
export const heat = {
  step_lat: 0.001,
  step_lon: 0.008,
  stops: [1, 8, 23, 60, 120, 250],
  cells: Array.from({ length: 180 }, (_, i) => [
    50775 + (i % 10),
    -164 + Math.floor(i / 10),
    [2, 10, 40, 90, 180, 300][Math.floor(i / 30)],
  ]),
};
export const vesselTrack = (mmsi) => {
  const vessel =
    snapshot.vessels.find((v) => String(v.mmsi) === String(mmsi)) ??
    snapshot.vessels[0];
  return {
    mmsi,
    count: 8,
    track: Array.from({ length: 8 }, (_, i) => ({
      lat: vessel.lat,
      lon: vessel.lon - i * 0.003,
    })),
  };
};
