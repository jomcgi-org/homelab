import { describe, expect, it } from "vitest";
import {
  STALE_AFTER,
  ciState,
  mergeRead,
  missingReadings,
} from "./dashboard-read.js";

const full = {
  today: { events: [] },
  health: { healthy: true, scanned: 3 },
  github: { open_prs: [] },
  factory: { ok: true, state: "enabled" },
};

describe("mergeRead", () => {
  it("replaces the sections a refresh brings", () => {
    const next = { ...full, health: { healthy: false, unhealthy: {} } };
    const { read, misses } = mergeRead(full, next);
    expect(read.health.healthy).toBe(false);
    expect(misses.health).toBe(0);
  });

  it("keeps a missing section's last good reading, then drops it", () => {
    let state = { read: full, misses: {} };
    for (let tick = 1; tick < STALE_AFTER; tick++) {
      state = mergeRead(state.read, { ...full, factory: null }, state.misses);
      expect(state.read.factory).toEqual(full.factory);
      expect(state.misses.factory).toBe(tick);
    }
    state = mergeRead(state.read, { ...full, factory: null }, state.misses);
    expect(state.read.factory).toBeNull();
    expect(state.read.health).toEqual(full.health);
  });

  it("clears the count when the section answers again", () => {
    const missed = mergeRead(full, { ...full, factory: null });
    const back = mergeRead(missed.read, full, missed.misses);
    expect(back.misses.factory).toBe(0);
  });

  it("treats a section carrying {error} as missing", () => {
    const { read, misses } = mergeRead(full, {
      ...full,
      github: { error: "rate limited" },
    });
    expect(read.github).toEqual(full.github);
    expect(misses.github).toBe(1);
  });

  it("changes nothing for a refresh that is not a read", () => {
    for (const next of [null, undefined, "x", { error: "down" }]) {
      expect(mergeRead(full, next, { factory: 2 })).toEqual({
        read: full,
        misses: { factory: 2 },
      });
    }
  });
});

describe("ciState", () => {
  it("reads the aggregate's words and GitHub's", () => {
    expect(ciState("passing")).toBe("ok");
    expect(ciState("success")).toBe("ok");
    expect(ciState("failing")).toBe("bad");
    expect(ciState("failure")).toBe("bad");
    expect(ciState("pending")).toBe("pending");
    expect(ciState(undefined)).toBe("pending");
  });
});

describe("missingReadings", () => {
  it("names each reading that did not answer", () => {
    expect(missingReadings(full)).toEqual([]);
    expect(missingReadings({ factory: full.factory })).toEqual([
      "cluster",
      "github",
    ]);
    expect(missingReadings({ ...full, health: { error: "x" } })).toEqual([
      "cluster",
    ]);
    expect(missingReadings(null)).toEqual(["cluster", "factory", "github"]);
  });
});
