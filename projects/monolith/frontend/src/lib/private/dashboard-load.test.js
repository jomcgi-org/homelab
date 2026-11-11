import { describe, expect, it, vi } from "vitest";
import {
  createBackendCache,
  readBoard,
  readFactorySummary,
  BOARD_STALE_MS,
} from "./backend-cache.js";
import { loadDashboard } from "./dashboard-load.js";

function jsonResponse(body, ok = true) {
  return { ok, status: ok ? 200 : 503, json: async () => body };
}

const home = { today: { events: [] }, health: { healthy: true, scanned: 3 } };
const board = {
  ok: true,
  state: "enabled",
  active: [{}],
  queued: [],
  active_count: 1,
  queued_count: 0,
  paused_count: 0,
  escalations: [{ receipt_id: 1, open: true, title: "t" }],
};
const tick = () => new Promise((r) => setTimeout(r, 0));

function cacheWith(routes) {
  const fetchImpl = vi.fn(async (url) => {
    const key = Object.keys(routes).find((path) => url.endsWith(path));
    return routes[key]();
  });
  return { cache: createBackendCache({ fetchImpl }), fetchImpl };
}

describe("loadDashboard", () => {
  it("keeps stale summaries on failure, expires them, and cannot poison full boards", async () => {
    let clock = 1_000,
      fail = false;
    const fetchImpl = vi.fn(async (url) => {
      if (url.endsWith("/summary")) return jsonResponse(board, !fail);
      if (url.endsWith("/api/agents/factory"))
        return jsonResponse({ full: true, nodes: [1] });
      return jsonResponse(home);
    });
    const cache = createBackendCache({ fetchImpl, now: () => clock });
    await loadDashboard("http://b", { cache });
    await tick();
    expect(
      fetchImpl.mock.calls.some(([url]) => url.endsWith("/api/agents/factory")),
    ).toBe(false);
    expect(await readBoard("http://b", { cache, coldWaitMs: 100 })).toEqual({
      full: true,
      nodes: [1],
    });
    expect(await readFactorySummary("http://b", { cache })).toEqual(board);
    fail = true;
    clock += 60_000;
    await loadDashboard("http://b", { cache });
    await tick();
    expect(
      (await loadDashboard("http://b", { cache })).dashboard.factory,
    ).toMatchObject({ inFlight: 1, readAgoMs: 60_000 });
    clock += BOARD_STALE_MS;
    await loadDashboard("http://b", { cache });
    await tick();
    expect(
      (await loadDashboard("http://b", { cache })).dashboard,
    ).toMatchObject({ factory: null, factory_pending: false });
  });
  it("merges the home aggregate with a factory summary from the cache", async () => {
    const { cache, fetchImpl } = cacheWith({
      "/api/home/dashboard": () => jsonResponse(home),
      "/api/agents/factory/summary": () => jsonResponse(board),
    });
    await loadDashboard("http://b", { cache });
    await tick();
    const { dashboard } = await loadDashboard("http://b", { cache });
    expect(dashboard.health).toEqual(home.health);
    expect(dashboard.factory).toMatchObject({ state: "enabled", inFlight: 1 });
    expect(dashboard.factory.waiting).toHaveLength(1);
    expect(fetchImpl.mock.calls.map(([url]) => url).sort()).toEqual([
      "http://b/api/agents/factory/summary",
      "http://b/api/home/dashboard",
    ]);
  });

  it("never waits on the board", async () => {
    const { cache } = cacheWith({
      "/api/home/dashboard": () => jsonResponse(home),
      "/api/agents/factory/summary": () => new Promise(() => {}),
    });
    const { dashboard } = await loadDashboard("http://b", { cache });
    // The board is on its way: "reading", not "unavailable".
    expect(dashboard).toEqual({
      ...home,
      factory: null,
      factory_pending: true,
    });
  });

  it("is null only when neither source has a reading", async () => {
    const { cache } = cacheWith({
      "/api/home/dashboard": () => jsonResponse({ error: "broken" }),
      "/api/agents/factory/summary": () => jsonResponse({}, false),
    });
    await loadDashboard("http://b", { cache });
    await tick();
    expect(await loadDashboard("http://b", { cache })).toEqual({
      dashboard: null,
    });
  });

  it("reads a malformed board as no factory reading", async () => {
    const { cache } = cacheWith({
      "/api/home/dashboard": () => jsonResponse(home),
      "/api/agents/factory/summary": () =>
        jsonResponse({ ok: true, active: 5, escalations: "x" }),
    });
    await loadDashboard("http://b", { cache });
    await tick();
    const { dashboard } = await loadDashboard("http://b", { cache });
    expect(dashboard).toEqual({
      ...home,
      factory: null,
      factory_pending: false,
    });
  });

  it("says how old the held board is", async () => {
    let clock = 1_000;
    const fetchImpl = vi.fn(async (url) =>
      url.endsWith("/api/agents/factory/summary")
        ? jsonResponse(board)
        : jsonResponse(home),
    );
    const cache = createBackendCache({ fetchImpl, now: () => clock });
    await loadDashboard("http://b", { cache });
    await tick();
    clock += 90_000;
    const { dashboard } = await loadDashboard("http://b", { cache });
    expect(dashboard.factory.readAgoMs).toBe(90_000);
    expect(dashboard.factory_pending).toBe(false);
  });

  it("reads a stalled board as unavailable once a read has failed", async () => {
    let clock = 1_000;
    let calls = 0;
    let failFirst;
    const fetchImpl = vi.fn(async (url) => {
      if (!url.endsWith("/api/agents/factory/summary"))
        return jsonResponse(home);
      calls += 1;
      // The first read fails when told to; the retry never answers.
      return calls === 1
        ? new Promise((r) => (failFirst = () => r(jsonResponse({}, false))))
        : new Promise(() => {});
    });
    const cache = createBackendCache({ fetchImpl, now: () => clock });
    const first = await loadDashboard("http://b", { cache });
    expect(first.dashboard.factory_pending).toBe(true);
    failFirst();
    await tick();
    clock += 60_000;
    const { dashboard } = await loadDashboard("http://b", { cache });
    expect(calls).toBe(2);
    expect(dashboard.factory).toBeNull();
    expect(dashboard.factory_pending).toBe(false);
  });
});
