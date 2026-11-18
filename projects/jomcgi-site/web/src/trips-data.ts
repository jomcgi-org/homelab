import { useEffect, useState } from "react";
import type { Boot, Latest, Payload, Trip } from "./trips-types";

export const EMPTY: Boot = {
  renderedAt: null,
  asOf: null,
  refreshSeconds: 86400,
  photoBase: "/data/trips/photos",
  index: [],
  days: [],
  photos: [],
  photoCount: 0,
  series: [],
  distance: 0,
};
export function readBoot(): Boot {
  if (typeof document === "undefined") return EMPTY;
  const content = document.getElementById("trips-data")?.textContent;
  return content ? (JSON.parse(content) as Boot) : EMPTY;
}
export function photoUrl(
  base: string,
  key: string,
  preset = "gallery",
): string {
  return `${base}/${encodeURIComponent(key)}/${preset}.webp`;
}
export function isStale(
  asOf: string | null,
  refresh: number,
  now = Date.now(),
) {
  return !!asOf && now - Date.parse(asOf) >= 3 * refresh * 1000;
}
async function json<T>(url: string): Promise<T> {
  if (!/^\/data\/trips\/(latest\.json|v\/[a-z0-9-]+\.json)$/.test(url))
    throw new Error("Invalid trip snapshot URL");
  const response = await fetch(url);
  if (!response.ok) throw new Error("Trip snapshot refresh failed");
  return response.json() as Promise<T>;
}
export function useTrip(boot: Boot) {
  const [latest, setLatest] = useState<Latest | null>(null);
  const [index, setIndex] = useState<Trip[] | null>(null);
  const [payload, setPayload] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [now, setNow] = useState<number | null>(boot.renderedAt);
  // Pages parse the boot data on each render, so boot.trip is a new object
  // every time. Depend on a stable value, or each refresh re-runs the effect.
  const hasTrip = !!boot.trip;
  useEffect(() => {
    if (!boot.asOf) return;
    let active = true;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 60000);
    json<Latest>("/data/trips/latest.json")
      .then(async (value) => {
        if (active) setLatest(value);
        if (!hasTrip && value.files.index) {
          const fresh = await json<{ trips: Trip[] }>(value.files.index);
          if (active) setIndex(fresh.trips);
        }
      })
      .catch(() => {
        if (active) setError("Refresh failed. Showing the saved trip data.");
      });
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [boot.asOf, hasTrip]);
  async function load() {
    if (payload) return payload;
    try {
      const manifest =
        latest ?? (await json<Latest>("/data/trips/latest.json"));
      const url = manifest.files.trips[boot.trip!.slug];
      if (!url) throw new Error("Trip missing from snapshot");
      const value = await json<Payload>(url);
      setLatest(manifest);
      setPayload(value);
      setError("");
      return value;
    } catch {
      setError("Refresh failed. Showing the saved trip data. Try again.");
      throw new Error("Trip data could not be loaded");
    }
  }
  return {
    load,
    index,
    payload,
    error,
    now,
    asOf: payload || index ? (latest?.asOf ?? boot.asOf) : boot.asOf,
    photoBase: latest && (payload || index) ? latest.photoBase : boot.photoBase,
  };
}
