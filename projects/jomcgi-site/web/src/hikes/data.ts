import { timestamp } from "../../../data/hikes-freshness.mjs";
export { REFRESH_MS, isStale } from "../../../data/hikes-freshness.mjs";
import type { Hour } from "./viability";

export interface Walk {
  id: string;
  name: string;
  url: string;
  lat: number;
  lon: number;
  distance_km: number;
  ascent_m: number;
  duration_h: number;
  hours: Hour[];
  forecast_fetched_at: string | null;
}
export interface Snapshot {
  asOf: string | null;
  generatedAt: string | null;
  fetchedAt: string | null;
  walks: Walk[];
}

export function parseWalks(value: unknown): Walk[] {
  const data = value as { schema_version?: number; walks?: unknown[] };
  if (data?.schema_version !== 2 || !Array.isArray(data.walks))
    throw new Error("Unsupported walks data");
  return data.walks
    .flatMap((row) => {
      const w = row as Walk;
      if (
        !w ||
        typeof w.id !== "string" ||
        typeof w.name !== "string" ||
        typeof w.url !== "string" ||
        !/^https:\/\/(www\.)?walkhighlands\.co\.uk\//.test(w.url) ||
        ![w.lat, w.lon, w.distance_km, w.ascent_m, w.duration_h].every(
          Number.isFinite,
        ) ||
        Math.abs(w.lat) > 90 ||
        Math.abs(w.lon) > 180 ||
        w.duration_h <= 0 ||
        !Array.isArray(w.hours)
      )
        return [];
      // Copy only facts; never retain or render scraped route prose.
      return [
        {
          id: w.id,
          name: w.name,
          url: w.url,
          lat: w.lat,
          lon: w.lon,
          distance_km: w.distance_km,
          ascent_m: w.ascent_m,
          duration_h: w.duration_h,
          forecast_fetched_at: timestamp(w.forecast_fetched_at),
          hours: w.hours.filter(
            (h) =>
              Array.isArray(h) &&
              h.length === 7 &&
              h.slice(0, 5).every(Number.isFinite),
          ),
        },
      ];
    })
    .sort((a, b) => a.name.localeCompare(b.name));
}

export async function loadWalks(signal: AbortSignal): Promise<Snapshot> {
  const latest = await fetch("/data/hikes/latest.json", {
    signal,
    cache: "no-cache",
  });
  if (!latest.ok) throw new Error("Could not load walks index");
  const index = await latest.json();
  const path = index.files?.walks;
  if (
    typeof path !== "string" ||
    !/^\/data\/hikes\/v\/walks-[a-f0-9]{12}\.json$/.test(path)
  )
    throw new Error("Invalid walks path");
  if (
    index.asOf !== null &&
    (typeof index.asOf !== "string" || !Number.isFinite(Date.parse(index.asOf)))
  )
    throw new Error("Invalid walks timestamp");
  const response = await fetch(path, { signal });
  if (!response.ok) throw new Error("Could not load walks");
  const payload = await response.json();
  return {
    asOf: index.asOf,
    generatedAt: timestamp(index.generatedAt),
    fetchedAt: timestamp(payload?.fetched_at),
    walks: parseWalks(payload),
  };
}
