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
