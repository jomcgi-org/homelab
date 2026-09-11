import { afterEach, describe, expect, it, vi } from "vitest";
import {
  STATS_AGE_TICK_INTERVAL_MS,
  STATS_REFRESH_INTERVAL_MS,
  buildMarquee,
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
