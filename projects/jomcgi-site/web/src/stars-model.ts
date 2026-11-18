// Ported from the hub’s stars/heat.js and urlParams.js. Dates remain UK viewing
// nights: after-midnight hours belong to the preceding evening.
export interface Hour {
  time: string;
  dark?: boolean;
  cloud_area_fraction: number;
  [key: string]: unknown;
}
export interface Site {
  id: string | number;
  name?: string;
  lat: number;
  lon: number;
  best_hours?: Hour[];
  clear_dark_hours?: number;
  clear_twilight_hours?: number;
  dark_hours?: number;
  clear_rate?: number;
  clear?: number[];
  dark?: number[];
  [key: string]: unknown;
}
export interface Snapshot {
  fetched_at?: string | null;
  sites: Site[];
  count?: number;
  darkness?: string;
  [key: string]: unknown;
}
export interface DataIndex {
  asOf: string | null;
  generatedAt?: string;
  files: { sites: string; historyMap: string; map: string; summary: string };
  sources?: { historyMap?: { lastModified?: string | null } };
}
export interface View {
  mode: "live" | "historical";
  selectedNight: string;
  selectedMonth: number;
}
export const DEFAULT_VIEW: View = {
  mode: "live",
  selectedNight: "all",
  selectedMonth: 0,
};
export const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];

// Preserve all fields, including future fields; share only identical records.
export function shareForecastHours(snapshot: Snapshot): Snapshot {
  const hours = new Map<string, Hour>();
  return {
    ...snapshot,
    sites: snapshot.sites.map((site) => {
      if (!Array.isArray(site.best_hours)) return site;
      return {
        ...site,
        best_hours: site.best_hours.map((hour) => {
          const key = JSON.stringify(hour);
          if (!hours.has(key)) hours.set(key, hour);
          return hours.get(key)!;
        }),
      };
    }),
  };
}

export function liveWindows(site: Site, now: number) {
  return (site.best_hours ?? []).filter((h) => {
    const t = Date.parse(h.time);
    return Number.isNaN(t) || t + 3_600_000 > now;
  });
}
const nightFormatter = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/London",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
const nightKeys = new Map<number, string>();
export function nightKey(iso: string): string | null {
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  if (nightKeys.has(ms)) return nightKeys.get(ms)!;
  // Locale defaults are not an ISO-date contract (en-CA differs across ICU
  // versions). Assemble the key from named UK-local parts instead.
  const parts = Object.fromEntries(
    nightFormatter
      .formatToParts(new Date(ms - 12 * 3_600_000))
      .map(({ type, value }) => [type, value]),
  );
  const key = `${parts.year}-${parts.month}-${parts.day}`;
  if (nightKeys.size >= 1024) nightKeys.clear();
  nightKeys.set(ms, key);
  return key;
}
export function starsNights(
  sites: Site[],
  darkness: string,
  now: number,
): string[] {
  const keys = new Set<string>();
  for (const site of sites)
    for (const hour of liveWindows(site, now)) {
      if (darkness !== "twilight" && hour.dark === false) continue;
      const key = nightKey(hour.time);
      if (key) keys.add(key);
    }
  return [...keys].sort();
}
export function selectedWindows(
  site: Site,
  darkness: string,
  nights: Set<string>,
  now: number,
) {
  return liveWindows(site, now).filter(
    (h) =>
      (darkness === "twilight" || h.dark !== false) &&
      (!nights.size || nights.has(nightKey(h.time) ?? "")),
  );
}
export function liveHeat(
  site: Site,
  darkness: string,
  nights: Set<string>,
  now: number,
) {
  const hours = selectedWindows(site, darkness, nights, now);
  if (!nights.size) return hours.length;
  const counts = new Map<string | null, number>();
  for (const h of hours) {
    const key = nightKey(h.time);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return Math.max(0, ...counts.values());
}
export function historyView(sites: Site[], month: number): Site[] {
  const sum = (values: number[] = []) =>
    values.reduce((n, v) => n + (Number.isFinite(v) ? v : 0), 0);
  return sites
    .map((site) => {
      const clear = month ? (site.clear?.[month - 1] ?? 0) : sum(site.clear);
      const dark = month ? (site.dark?.[month - 1] ?? 0) : sum(site.dark);
      return {
        ...site,
        clear_dark_hours: clear,
        dark_hours: dark,
        clear_rate: dark > 0 ? clear / dark : 0,
      };
    })
    .filter((s) => s.dark_hours > 0)
    .sort((a, b) => b.clear_dark_hours - a.clear_dark_hours);
}
export function monthBars(months: number[] = []) {
  const values = MONTHS.map((_, i) =>
    Number.isFinite(months[i]) && months[i]! > 0 ? months[i]! : 0,
  );
  const max = Math.max(0, ...values);
  return values.map((value, i) => ({
    month: MONTHS[i]!,
    value,
    frac: max ? value / max : 0,
    isMax: max > 0 && value === max,
  }));
}
export function readStarsParams(params: URLSearchParams): View {
  const month = Number(params.get("month"));
  const night = params.get("night") ?? "";
  return {
    mode: params.get("mode") === "historical" ? "historical" : "live",
    selectedNight: /^\d{4}-\d{2}-\d{2}$/.test(night) ? night : "all",
    selectedMonth:
      Number.isInteger(month) && month >= 1 && month <= 12 ? month : 0,
  };
}
export function writeStarsParams(params: URLSearchParams, view: View) {
  for (const key of ["mode", "night", "month"]) params.delete(key);
  if (view.mode === "historical") {
    params.set("mode", view.mode);
    if (view.selectedMonth) params.set("month", String(view.selectedMonth));
  } else if (view.selectedNight !== "all")
    params.set("night", view.selectedNight);
  return params;
}
export const siteName = (site: Site) =>
  site.name || `${site.lat.toFixed(3)}°, ${site.lon.toFixed(3)}°`;
export const nightLabel = (night: string) =>
  new Date(`${night}T12:00:00Z`).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    timeZone: "Europe/London",
  });

// Do not permit an index to send the browser to the hub, an R2 API or another
// origin. Files must be immutable members of this section’s snapshot.
export function dataPath(path: string): string {
  if (!/^\/data\/stars\/v\/[a-zA-Z]+-[a-f0-9]{12}\.json$/.test(path))
    throw new Error("Invalid stars data path");
  return path;
}
export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { signal: AbortSignal.timeout(30_000) });
  if (!response.ok) throw new Error(`Data request failed (${response.status})`);
  return response.json();
}
