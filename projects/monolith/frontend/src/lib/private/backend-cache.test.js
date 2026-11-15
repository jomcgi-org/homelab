import { describe, expect, it, vi } from "vitest";
import { createBackendCache, startPrivatePrewarm } from "./backend-cache.js";

const ok = (body) => ({ ok: true, json: async () => body });
const down = () => ({ ok: false, json: async () => ({}) });
const tick = () => new Promise((r) => setTimeout(r, 0));

function clock() {
  let t = 1_000_000;
  return { now: () => t, advance: (ms) => (t += ms) };
}

describe("createBackendCache", () => {
  it("serves the held value at once and refreshes it in the background", async () => {
    const c = clock();
    let release;
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(ok({ v: 1 }))
      .mockImplementationOnce(
        () => new Promise((r) => (release = () => r(ok({ v: 2 })))),
      );
    const cache = createBackendCache({ fetchImpl, now: c.now });
    expect(await cache.read("k", "/k", { coldWaitMs: 50 })).toEqual({ v: 1 });
    c.advance(60000);
    expect(await cache.read("k", "/k")).toEqual({ v: 1 });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    release();
    await tick();
    expect(await cache.read("k", "/k")).toEqual({ v: 2 });
  });

  it("waits a bounded time only when cold", async () => {
    const cache = createBackendCache({
      fetchImpl: () => new Promise(() => {}),
    });
    const started = performance.now();
    expect(await cache.read("k", "/k", { coldWaitMs: 30 })).toBeNull();
    expect(performance.now() - started).toBeLessThan(1000);
  });

  it("runs one read per key at a time and spaces failed attempts", async () => {
    const c = clock();
    const fetchImpl = vi.fn(async () => down());
    const cache = createBackendCache({ fetchImpl, now: c.now });
    await cache.read("k", "/k");
    await cache.read("k", "/k");
    await tick();
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    c.advance(60000);
    await cache.read("k", "/k");
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it("keeps a value through failures, then drops it when stale", async () => {
    const c = clock();
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(ok({ v: 1 }))
      .mockResolvedValue(down());
    const cache = createBackendCache({ fetchImpl, now: c.now });
    await cache.read("k", "/k", { coldWaitMs: 50 });
    c.advance(120000);
    expect(await cache.read("k", "/k", { staleMs: 300000 })).toEqual({ v: 1 });
    c.advance(200000);
    expect(await cache.read("k", "/k", { staleMs: 300000 })).toBeNull();
  });

  it("treats {error} bodies and throws as no reading, without rejecting", async () => {
    const rejections = [];
    const onReject = (reason) => rejections.push(reason);
    process.on("unhandledRejection", onReject);
    try {
      const cache = createBackendCache({
        fetchImpl: vi
          .fn()
          .mockResolvedValueOnce(ok({ error: "x" }))
          .mockRejectedValueOnce(new Error("offline")),
      });
      expect(await cache.read("a", "/a", { coldWaitMs: 50 })).toBeNull();
      expect(await cache.read("b", "/b", { coldWaitMs: 50 })).toBeNull();
      await tick();
    } finally {
      process.off("unhandledRejection", onReject);
    }
    expect(rejections).toEqual([]);
  });

  it("refreshes on the next read after invalidate, still serving the old value", async () => {
    const c = clock();
    const fetchImpl = vi
      .fn()
      .mockResolvedValueOnce(ok({ v: 1 }))
      .mockResolvedValueOnce(ok({ v: 2 }));
    const cache = createBackendCache({ fetchImpl, now: c.now });
    await cache.read("k", "/k", { coldWaitMs: 50 });
    cache.invalidate("k");
    expect(await cache.read("k", "/k")).toEqual({ v: 1 });
    await tick();
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(await cache.read("k", "/k")).toEqual({ v: 2 });
  });

  it("reports the held value's age and an in-flight read", async () => {
    const c = clock();
    let resolve;
    const fetchImpl = vi.fn(
      () => new Promise((r) => (resolve = () => r(ok({ v: 1 })))),
    );
    const cache = createBackendCache({ fetchImpl, now: c.now });
    expect(cache.status("k")).toEqual({
      ageMs: null,
      pending: false,
      failed: false,
    });
    await cache.read("k", "/k");
    expect(cache.status("k")).toEqual({
      ageMs: null,
      pending: true,
      failed: false,
    });
    resolve();
    await tick();
    c.advance(5000);
    expect(cache.status("k")).toEqual({
      ageMs: 5000,
      pending: false,
      failed: false,
    });
  });
});

describe("startPrivatePrewarm", () => {
  it("does nothing unless enabled with an API base", () => {
    const fetchImpl = vi.fn();
    const cache = createBackendCache({ fetchImpl });
    expect(
      startPrivatePrewarm({ enabled: false, apiBase: "http://b", cache }),
    ).toBeNull();
    expect(
      startPrivatePrewarm({ enabled: true, apiBase: "", cache }),
    ).toBeNull();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("reads the board and home at once and on each tick", async () => {
    vi.useFakeTimers();
    try {
      const fetchImpl = vi.fn(async () => ok({ v: 1 }));
      let t = 0;
      const cache = createBackendCache({ fetchImpl, now: () => t });
      const timer = startPrivatePrewarm({
        enabled: true,
        apiBase: "http://b",
        everyMs: 1000,
        cache,
      });
      const urls = () => fetchImpl.mock.calls.map(([url]) => url).sort();
      expect(urls()).toEqual([
        "http://b/api/agents/factory/summary",
        "http://b/api/home/dashboard",
      ]);
      // Let the first reads land before the clock moves on.
      await vi.advanceTimersByTimeAsync(10);
      t += 120_000;
      await vi.advanceTimersByTimeAsync(1000);
      expect(fetchImpl).toHaveBeenCalledTimes(4);
      clearInterval(timer);
    } finally {
      vi.useRealTimers();
    }
  });
});
