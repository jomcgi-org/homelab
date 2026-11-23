import { useEffect, useState } from "react";
import type {
  Documents,
  Goal,
  Issue,
  Manifest,
  ObjectPath,
  Session,
  Stat,
  Story,
} from "../../../../factory-storyboard/schema/types";
import { validate } from "../../../../factory-storyboard/schema/validate";

export interface Snapshot {
  manifest: Manifest;
  story: Story | null;
  goals: Record<string, Goal | null>;
  issues: Record<string, Issue | null>;
  sessions: Record<string, Session | null>;
}
export const paths = {
  goal: (id: string) => `/factory/goals/${encodeURIComponent(id)}`,
  issue: (key: string) => `/factory/issues/${encodeURIComponent(key)}`,
  session: (id: string) => `/factory/sessions/${encodeURIComponent(id)}`,
  pr: (number: number) =>
    `https://github.com/jomcgi-org/homelab/pull/${number}`,
};

export function fillStats(text: string, stats: Record<string, Stat>): string {
  return text.replace(/\{\{stat:([a-z][a-zA-Z0-9_]*)\}\}/g, (_, key: string) =>
    Object.hasOwn(stats, key) ? formatNumber(stats[key]!.value) : "–",
  );
}
export const formatNumber = (value: number) =>
  new Intl.NumberFormat("en-GB", { maximumFractionDigits: 2 }).format(value);

export async function sha256(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}

// Only successful, validated immutable objects are cached. Failed reads retry
// at the next manifest poll, including when the manifest has not changed.
export function createLoader() {
  const cache = new Map<ObjectPath, unknown>();
  async function read(url: string, signal: AbortSignal, immutable = false) {
    const response = await fetch(url, {
      signal: AbortSignal.any([signal, AbortSignal.timeout(15_000)]),
      cache: immutable ? "force-cache" : "no-store",
      credentials: "omit",
    });
    if (!response.ok) throw new Error("Snapshot not available");
    return response;
  }
  async function object<K extends Exclude<keyof Documents, "manifest">>(
    kind: K,
    path: ObjectPath,
    signal: AbortSignal,
    id?: string,
  ): Promise<Documents[K] | null> {
    try {
      let value = cache.get(path);
      if (value === undefined) {
        const bytes = await (
          await read(`/data/factory/${path}`, signal, true)
        ).arrayBuffer();
        if (`objects/${await sha256(bytes)}.json` !== path) return null;
        value = JSON.parse(
          new TextDecoder("utf-8", { fatal: true }).decode(bytes),
        ) as unknown;
      }
      if (!validate(kind, value)) return null;
      if (kind !== "story") {
        const entity = value as Goal | Issue | Session;
        if ((entity.kind === "issue" ? entity.key : entity.id) !== id)
          return null;
      }
      cache.set(path, value);
      return value;
    } catch {
      return null;
    }
  }
  async function collection<K extends "goal" | "issue" | "session">(
    kind: K,
    entries: Record<string, ObjectPath>,
    signal: AbortSignal,
  ): Promise<Record<string, Documents[K] | null>> {
    return Object.fromEntries(
      await Promise.all(
        Object.entries(entries).map(
          async ([id, path]) =>
            [id, await object(kind, path, signal, id)] as const,
        ),
      ),
    );
  }
  return async (signal: AbortSignal): Promise<Snapshot> => {
    const manifest: unknown = await (
      await read("/data/factory/manifest.json", signal)
    ).json();
    if (!validate("manifest", manifest))
      throw new Error("Manifest not available");
    const current = new Set([
      manifest.story,
      ...Object.values(manifest.goals),
      ...Object.values(manifest.issues),
      ...Object.values(manifest.sessions),
    ]);
    for (const path of cache.keys()) if (!current.has(path)) cache.delete(path);
    const [story, goals, issues, sessions] = await Promise.all([
      object("story", manifest.story, signal),
      collection("goal", manifest.goals, signal),
      collection("issue", manifest.issues, signal),
      collection("session", manifest.sessions, signal),
    ]);
    signal.throwIfAborted();
    return { manifest, story, goals, issues, sessions };
  };
}

export function useSnapshot() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const load = createLoader();
    let pending = false;
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const next = await load(controller.signal);
        if (!controller.signal.aborted) {
          setSnapshot(next);
          setError(false);
        }
      } catch {
        if (!controller.signal.aborted) setError(true);
      } finally {
        pending = false;
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 60_000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, []);
  return { snapshot, error };
}

const short = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 });
/** Headline figures and chart labels: one decimal place at most. */
export const formatShort = (value: number) => short.format(value);

const instant = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
  timeZone: "UTC",
});
export function formatInstant(value: string): string {
  const time = Date.parse(value);
  return Number.isNaN(time) ? value : `${instant.format(time)} UTC`;
}
const day = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});
/** "Week of 2026-11-09" and "2026-11-09" both read "9 Nov" on an axis. */
export function axisLabel(label: string): string {
  const date = /(\d{4}-\d{2}-\d{2})$/.exec(label)?.[1];
  const time = date ? Date.parse(`${date}T00:00:00Z`) : NaN;
  return Number.isNaN(time) ? label : day.format(time);
}
/** A generated session id shows its first eight digits; a named one stays. */
export const sessionLabel = (id: string) =>
  /^session-[a-f0-9]{16,}$/.test(id) ? id.slice(8, 16) : id;

export function median(values: readonly number[]): number | undefined {
  if (!values.length) return undefined;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = sorted.length >> 1;
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1]! + sorted[middle]!) / 2;
}
export type Tally = { label: string; value: number };
const hourBins: [string, number][] = [
  ["Under 6 h", 6],
  ["6–12 h", 12],
  ["12–24 h", 24],
  ["1–2 d", 48],
  ["2–4 d", 96],
  ["Over 4 d", Infinity],
];
/** Counts durations in fixed bins, so one chart reads the same at any size. */
export function tallyHours(values: readonly number[]): Tally[] {
  const counts = hourBins.map(() => 0);
  for (const value of values)
    counts[hourBins.findIndex(([, limit]) => value < limit)]!++;
  return hourBins.map(([label], index) => ({ label, value: counts[index]! }));
}
/** Counts whole numbers from 1 to the largest value, at most eight columns. */
export function tallyCounts(values: readonly number[]): Tally[] {
  if (!values.length) return [];
  const last = Math.min(8, Math.max(1, ...values.map(Math.round)));
  const counts = Array.from({ length: last }, () => 0);
  let over = false;
  for (const value of values) {
    const count = Math.max(1, Math.round(value));
    if (count > last) over = true;
    counts[Math.min(last, count) - 1]!++;
  }
  return counts.map((value, index) => ({
    label: index === last - 1 && over ? `${last}+` : String(index + 1),
    value,
  }));
}

/* Sentences written from the snapshot. Each states one fact, or nothing
 * when the snapshot has no measurement for it. */
const duration = (hours: number) =>
  hours < 48
    ? `${formatShort(hours)} ${hours === 1 ? "hour" : "hours"}`
    : `${formatShort(hours / 24)} days`;
export function shippedSentence(shipped: number, total: number) {
  return total > 0 ? `${shipped} of ${total} issues shipped.` : undefined;
}
export function weekSentence(points: readonly Tally[]) {
  const last = points.at(-1);
  const before = points.at(-2);
  if (!last) return undefined;
  const now = `${formatShort(last.value)} shipped in the week of ${axisLabel(last.label)}`;
  return before
    ? `${now}, after ${formatShort(before.value)} the week before.`
    : `${now}.`;
}
export function mergeSentence(hours: readonly number[]) {
  const middle = median(hours);
  return middle === undefined
    ? undefined
    : `Half of the issues merge within ${duration(middle)}.`;
}
export function roundsSentence(rounds: readonly number[]) {
  const middle = median(rounds);
  return middle === undefined
    ? undefined
    : `A typical PR takes ${formatShort(middle)} review ${middle === 1 ? "round" : "rounds"}.`;
}
export function firstPassSentence(points: readonly Tally[]) {
  const last = points.at(-1);
  return last
    ? `${formatShort(last.value)}% of PRs passed review the first time in the week of ${axisLabel(last.label)}.`
    : undefined;
}
