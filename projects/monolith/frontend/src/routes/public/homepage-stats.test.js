import { afterEach, describe, expect, it, vi } from "vitest";
import {
  STATS_AGE_TICK_INTERVAL_MS,
  STATS_REFRESH_INTERVAL_MS,
  buildMarquee,
  deployReadout,
  formatAgo,
  isStatsSnapshot,
  startHomepageStatsPolling,
} from "./homepage-stats.js";

const initialStats = {
  deploy: {
    latest_commit_sha: "old",
    deployed_at: "2026-09-11T00:00:00Z",
  },
};

const refreshedStats = {
  deploy: {
    latest_commit_sha: "new",
    deployed_at: "2026-09-11T00:05:00Z",
  },
};

const freshStats = {
  cluster: {
    nodes: 2,
    pods: 135,
    cpu_used_cores: 4.99,
    cpu_capacity_cores: 32,
    memory_used_gb: 62.5,
    memory_capacity_gb: 108,
    argocd_apps: 28,
  },
  knowledge: { facts: 1309, chunks: 5948, raw_inputs: 366 },
  deploy: { latest_commit_sha: "abc1234" },
  platform: { in_production_since: "2025-01" },
  cached_at: "2026-10-18T00:00:00Z",
};
const cachedStats = {
  ...freshStats,
  gpu: { utilization_pct: 50, memory_used_gb: 12, memory_total_gb: 24 },
};

describe("GPU-free stats contract", () => {
  it.each([freshStats, cachedStats])(
    "validates and renders non-GPU statistics from fresh or transitional snapshots",
    async (stats) => {
      expect(isStatsSnapshot(stats)).toBe(true);
      expect(buildMarquee(stats)).toEqual([
        "~/homelab",
        "2 nodes · 135 pods",
        "cpu: 4.99 / 32 cores",
        "mem: 62.5 / 108 gb",
        "argocd: 28 apps",
        `kg: ${(1309).toLocaleString()} notes`,
        "last commit: abc1234",
      ]);
    },
  );

  it("ignores a GPU-only legacy payload as an unusable snapshot", () => {
    const stats = { gpu: cachedStats.gpu };
    expect(isStatsSnapshot(stats)).toBe(false);
    expect(buildMarquee(stats)).toEqual(["~/homelab"]);
  });
});

describe("deployment readout", () => {
  it("shows nothing rather than a fabricated figure without a deploy time", () => {
    expect(deployReadout(null)).toBeNull();
    expect(deployReadout({ deploy: { latest_commit_sha: "abc" } })).toBeNull();
    expect(deployReadout({ deploy: { deployed_at: "not a date" } })).toBeNull();
  });

  it("formats the age and shortens the sha", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-11T00:10:00Z"));
    expect(formatAgo("2026-09-11T00:00:00Z")).toBe("10m");
    expect(deployReadout(refreshedStats)).toEqual({ ago: "5m", sha: "new" });
    expect(
      deployReadout({
        deploy: {
          deployed_at: "2026-09-10T00:00:00Z",
          latest_commit_sha: "0123456789",
        },
      }),
    ).toEqual({ ago: "24h", sha: "0123456" });
    expect(
      deployReadout({ deploy: { deployed_at: "2026-09-11T00:05:00Z" } }),
    ).toEqual({ ago: "5m", sha: null });
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("homepage stats polling", () => {
  it("refreshes the cached same-origin endpoint every five minutes", async () => {
    vi.useFakeTimers();
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => refreshedStats,
    });
    const render = vi.fn();
    const stop = startHomepageStatsPolling(initialStats, render, { fetchFn });

    await vi.advanceTimersByTimeAsync(STATS_REFRESH_INTERVAL_MS - 1);
    expect(fetchFn).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(fetchFn).toHaveBeenCalledWith("/app/notes/stats", {
      signal: expect.any(AbortSignal),
    });
    expect(render).toHaveBeenLastCalledWith(refreshedStats);

    stop();
  });

  it("keeps the age tick independent and redraws the refreshed snapshot", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-11T00:10:00Z"));
    const fetchFn = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => refreshedStats,
    });
    const render = vi.fn(buildMarquee);
    const stop = startHomepageStatsPolling(initialStats, render, { fetchFn });

    await vi.advanceTimersByTimeAsync(STATS_REFRESH_INTERVAL_MS);
    expect(render).toHaveLastReturnedWith(
      expect.arrayContaining(["last commit: new", "deployed 10m ago"]),
    );

    render.mockClear();
    await vi.advanceTimersByTimeAsync(STATS_AGE_TICK_INTERVAL_MS);
    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(render).toHaveBeenCalledOnce();
    expect(render).toHaveBeenCalledWith(refreshedStats);
    expect(render).toHaveLastReturnedWith(
      expect.arrayContaining(["last commit: new", "deployed 11m ago"]),
    );

    stop();
  });

  it.each([
    ["a rejected request", () => Promise.reject(new Error("offline"))],
    ["a non-OK response", async () => ({ ok: false })],
    [
      "a malformed response",
      async () => ({ ok: true, json: async () => ["not", "stats"] }),
    ],
    [
      "a GPU-only legacy response",
      async () => ({ ok: true, json: async () => ({ gpu: cachedStats.gpu }) }),
    ],
  ])("retains the usable snapshot after %s", async (_name, request) => {
    vi.useFakeTimers();
    const fetchFn = vi.fn(request);
    const render = vi.fn();
    const stop = startHomepageStatsPolling(initialStats, render, { fetchFn });

    await vi.advanceTimersByTimeAsync(STATS_REFRESH_INTERVAL_MS);
    render.mockClear();
    await vi.advanceTimersByTimeAsync(STATS_AGE_TICK_INTERVAL_MS);

    expect(render).toHaveBeenCalledWith(initialStats);
    stop();
  });

  it("does not overlap an unfinished refresh", () => {
    vi.useFakeTimers();
    const fetchFn = vi.fn(() => new Promise(() => {}));
    const stop = startHomepageStatsPolling(initialStats, vi.fn(), { fetchFn });

    vi.advanceTimersByTime(2 * STATS_REFRESH_INTERVAL_MS);

    expect(fetchFn).toHaveBeenCalledTimes(1);
    stop();
  });

  it("clears timers, aborts the request, and ignores a late response", async () => {
    vi.useFakeTimers();
    let resolveJson;
    let requestSignal;
    const fetchFn = vi.fn(async (_url, { signal }) => {
      requestSignal = signal;
      return {
        ok: true,
        json: () =>
          new Promise((resolve) => {
            resolveJson = resolve;
          }),
      };
    });
    const render = vi.fn();
    const stop = startHomepageStatsPolling(initialStats, render, { fetchFn });

    vi.advanceTimersByTime(STATS_REFRESH_INTERVAL_MS);
    await Promise.resolve();
    expect(fetchFn).toHaveBeenCalledTimes(1);

    const callsBeforeStop = render.mock.calls.length;
    stop();
    expect(requestSignal.aborted).toBe(true);

    resolveJson(refreshedStats);
    await Promise.resolve();
    await Promise.resolve();
    vi.advanceTimersByTime(2 * STATS_REFRESH_INTERVAL_MS);

    expect(fetchFn).toHaveBeenCalledTimes(1);
    expect(render).toHaveBeenCalledTimes(callsBeforeStop);
  });
});
