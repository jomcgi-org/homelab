import type { Walk } from "./data";

// Region centroids and 75 km radius copied from the hub's HikesApp.svelte.
export const LOCATIONS = [
  { key: "fort-william", label: "Fort William", lat: 56.8326, lon: -5.251 },
  { key: "cairngorms", label: "Cairngorms", lat: 57.1232, lon: -3.631 },
  { key: "argyll", label: "Argyll & Oban", lat: 56.1519, lon: -5.2638 },
  { key: "perthshire", label: "Perthshire", lat: 56.5572, lon: -3.8982 },
  { key: "galloway", label: "Galloway", lat: 55.0211, lon: -4.08 },
  { key: "skye", label: "Skye", lat: 57.3649, lon: -6.2075 },
  { key: "sutherland", label: "Sutherland", lat: 58.3045, lon: -4.1337 },
  { key: "ullapool", label: "Ullapool & Assynt", lat: 57.9288, lon: -4.9246 },
  { key: "loch-ness", label: "Loch Ness", lat: 57.4078, lon: -4.5199 },
  { key: "loch-lomond", label: "Loch Lomond", lat: 56.2231, lon: -4.5265 },
  { key: "glasgow", label: "Glasgow", lat: 55.6912, lon: -4.3392 },
  { key: "torridon", label: "Wester Ross", lat: 57.5841, lon: -5.5325 },
];
export const LIMITS = [
  ["dmin", "Min duration (h)", "0.5"],
  ["dmax", "Max duration (h)", "0.5"],
  ["kmin", "Min distance (km)", "1"],
  ["kmax", "Max distance (km)", "1"],
  ["ascent", "Max ascent (m)", "50"],
] as const;
export type Filters = Record<
  (typeof LIMITS)[number][0] | "near" | "day" | "walk",
  string
>;
export const DEFAULT_FILTERS: Filters = {
  dmin: "",
  dmax: "",
  kmin: "",
  kmax: "",
  ascent: "",
  near: "",
  day: "",
  walk: "",
};
export function readFilters(params: URLSearchParams): Filters {
  const result = { ...DEFAULT_FILTERS };
  for (const [key] of LIMITS) {
    const value = params.get(key);
    if (value && Number.isFinite(Number(value)) && Number(value) >= 0)
      result[key] = value;
  }
  const near = params.get("near");
  if (near === "__me__" || LOCATIONS.some((l) => l.key === near))
    result.near = near!;
  const day = params.get("day");
  if (day && /^\d{4}-\d{2}-\d{2}$/.test(day)) result.day = day;
  const walk = params.get("walk");
  if (
    walk &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(walk)
  )
    result.walk = walk;
  return result;
}
export function writeFilters(params: URLSearchParams, filters: Filters) {
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
    else params.delete(key);
  }
}
export type Center = { lat: number; lon: number };
export function distance(a: Center, b: Center) {
  const rad = Math.PI / 180;
  const h =
    Math.sin(((b.lat - a.lat) * rad) / 2) ** 2 +
    Math.cos(a.lat * rad) *
      Math.cos(b.lat * rad) *
      Math.sin(((b.lon - a.lon) * rad) / 2) ** 2;
  return 6371 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(Math.max(0, 1 - h)));
}
export type ViableWalk = Walk & { viable_days: string[] };
export function filterWalks(
  walks: ViableWalk[],
  f: Filters,
  center: Center | null,
) {
  const num = (value: string, fallback: number) =>
    value === "" ? fallback : Number(value);
  const selected = walks.filter(
    (w) =>
      w.duration_h >= num(f.dmin, 0) &&
      w.duration_h <= num(f.dmax, Infinity) &&
      w.distance_km >= num(f.kmin, 0) &&
      w.distance_km <= num(f.kmax, Infinity) &&
      w.ascent_m <= num(f.ascent, Infinity) &&
      (f.day ? w.viable_days.includes(f.day) : w.viable_days.length > 0),
  );
  if (!center) return selected;
  return selected
    .map((walk) => ({ walk, km: distance(center, walk) }))
    .filter(({ km }) => km <= 75)
    .sort((a, b) => a.km - b.km)
    .map(({ walk }) => walk);
}
export function effortCeilings(walks: Walk[]) {
  const p95 = (key: "duration_h" | "ascent_m" | "distance_km") => {
    const sorted = walks.map((w) => w[key]).sort((a, b) => a - b);
    return (
      sorted[Math.min(sorted.length - 1, Math.floor(0.95 * sorted.length))] || 1
    );
  };
  return {
    duration_h: p95("duration_h"),
    ascent_m: p95("ascent_m"),
    distance_km: p95("distance_km"),
  };
}
export function effort(
  walk: Walk,
  ceilings: ReturnType<typeof effortCeilings>,
) {
  return (
    (Object.keys(ceilings) as (keyof typeof ceilings)[]).reduce(
      (sum, key) => sum + Math.min(1, Math.max(0, walk[key] / ceilings[key])),
      0,
    ) / 3
  );
}
