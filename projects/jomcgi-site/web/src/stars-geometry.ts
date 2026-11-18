import type { GeoJSONSourceSpecification } from "maplibre-gl";
import { liveHeat, type Site } from "./stars-model";

export interface MapData {
  sites: Site[];
  mode: "live" | "historical";
  darkness: string;
  activeNights: Set<string>;
  now: number;
}
export function cells({
  sites,
  mode,
  darkness,
  activeNights,
  now,
}: MapData): GeoJSONSourceSpecification["data"] {
  const rows = sites
    .filter(
      (site) =>
        Number.isFinite(site.lat) &&
        Number.isFinite(site.lon) &&
        Math.abs(site.lat) <= 90 &&
        Math.abs(site.lon) <= 180 &&
        (site.lat !== 0 || site.lon !== 0),
    )
    .map((site) => ({
      site,
      heat:
        mode === "historical"
          ? (site.clear_dark_hours ?? 0)
          : liveHeat(site, darkness, activeNights, now),
    }))
    .filter(({ heat }) => heat > 0);
  const max = rows.reduce((value, { heat }) => Math.max(value, heat), 1);
  const step = (values: number[], fallback: number) => {
    const unique = [
      ...new Set(values.map((v) => Math.round(v * 1e6) / 1e6)),
    ].sort((a, b) => a - b);
    let min = Infinity;
    for (let i = 1; i < unique.length; i++) {
      const gap = unique[i]! - unique[i - 1]!;
      if (gap > 1e-6) min = Math.min(min, gap);
    }
    return Number.isFinite(min) ? min : fallback;
  };
  const dx =
    step(
      rows.map(({ site }) => site.lon),
      0.067,
    ) / 2;
  const dy =
    step(
      rows.map(({ site }) => site.lat),
      0.036,
    ) / 2;
  return {
    type: "FeatureCollection",
    features: rows.map(({ site, heat }) => ({
      type: "Feature",
      properties: { id: site.id, score: (100 * heat) / max },
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [site.lon - dx, site.lat - dy],
            [site.lon + dx, site.lat - dy],
            [site.lon + dx, site.lat + dy],
            [site.lon - dx, site.lat + dy],
            [site.lon - dx, site.lat - dy],
          ],
        ],
      },
    })),
  };
}
