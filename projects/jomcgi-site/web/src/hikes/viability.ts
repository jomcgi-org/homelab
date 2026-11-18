// Port of projects/monolith/hikes/router.py. Hours are daylight hours from
// walks.json v2; only the current clock hour and later may contribute.
export type Hour = [
  number,
  number,
  number,
  number,
  number,
  number | null,
  number | null,
];

const ukDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Europe/London",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});
export function ukDay(unix: number): string {
  // Locale output order is not a date contract (en-CA varies by ICU build).
  const parts = ukDate.formatToParts(new Date(unix * 1000));
  const value = (type: string) =>
    parts.find((part) => part.type === type)!.value;
  return `${value("year")}-${value("month")}-${value("day")}`;
}
export const cutoff = (now: number) => Math.floor(now / 3_600_000) * 3600;
export const isViable = (hour: Hour) => hour[2] <= 2 && hour[3] <= 80;

/** Sorted unique UTC hour buckets; bridge one missing hour, counting good hours only. */
export function longestGoodRun(indices: number[]): number {
  if (!indices.length) return 0;
  let best = 1;
  let start = 0;
  let gapAfter: number | null = null;
  for (let i = 1; i < indices.length; i++) {
    const delta = indices[i]! - indices[i - 1]!;
    if (delta === 2) {
      if (gapAfter !== null) start = gapAfter;
      gapAfter = i;
    } else if (delta !== 1) {
      start = i;
      gapAfter = null;
    }
    best = Math.max(best, i - start + 1);
  }
  return best;
}

/** Good-hour unix timestamps, bucketed on the UK calendar, as in _doable_days. */
export function doableDays(hours: number[], duration: number): string[] {
  if (!(duration > 0)) return [];
  const days = new Map<string, Set<number>>();
  for (const hour of hours) {
    if (!Number.isFinite(hour)) continue;
    const day = ukDay(hour);
    if (!days.has(day)) days.set(day, new Set());
    days.get(day)!.add(Math.floor(hour / 3600));
  }
  const shoulder = duration > 7 ? 2 : 0;
  return [...days]
    .filter(([, indices]) => {
      const run = longestGoodRun([...indices].sort((a, b) => a - b));
      return run > 0 && run + shoulder + 1e-9 >= 0.8 * duration;
    })
    .map(([day]) => day)
    .sort();
}

export function viableDays(
  hours: Hour[],
  duration: number,
  now: number,
): string[] {
  return doableDays(
    hours.filter((h) => h[0] >= cutoff(now) && isViable(h)).map((h) => h[0]),
    duration,
  );
}

export function upcomingDays(now: number): string[] {
  const anchor = new Date(`${ukDay(now / 1000)}T12:00:00Z`);
  return Array.from({ length: 7 }, (_, i) =>
    ukDay(anchor.getTime() / 1000 + i * 86_400),
  );
}

export const dayLabel = (day: string) =>
  new Date(`${day}T12:00:00Z`).toLocaleDateString("en-GB", {
    timeZone: "Europe/London",
    weekday: "short",
    day: "numeric",
    month: "short",
  });
