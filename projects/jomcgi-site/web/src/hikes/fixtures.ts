// Small deterministic forecast for the unit and rendered-page checks.
import type { Walk } from "./data";
export const FIXTURE_NOW = Date.parse("2026-06-15T06:00:00Z");
export const FIXTURE_WALK: Walk = {
  id: "aaaaaaaa-0000-5000-8000-000000000001",
  name: "Ben Vorlich from Ardlui",
  url: "https://www.walkhighlands.co.uk/lochlomond/ben-vorlich-ardlui.shtml",
  lat: 56.2734,
  lon: -4.7521,
  distance_km: 11,
  ascent_m: 920,
  duration_h: 5.5,
  forecast_fetched_at: new Date(FIXTURE_NOW).toISOString(),
  hours: Array.from({ length: 6 }, (_, i) => [
    FIXTURE_NOW / 1000 + i * 3600,
    12.5,
    0,
    14,
    40,
    null,
    null,
  ]),
};
