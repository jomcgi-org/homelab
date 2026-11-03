import { describe, expect, it, vi } from "vitest";
import { createBackendCache } from "./backend-cache.js";
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
  it("merges the home aggregate with a factory summary from the cache", async () => {
    const { cache, fetchImpl } = cacheWith({
      "/api/home/dashboard": () => jsonResponse(home),
      "/api/agents/factory": () => jsonResponse(board),
    });
    await loadDashboard("http://b", { cache });
    await tick();
    const { dashboard } = await loadDashboard("http://b", { cache });
    expect(dashboard.health).toEqual(home.health);
    expect(dashboard.factory).toMatchObject({ state: "enabled", inFlight: 1 });
    expect(dashboard.factory.waiting).toHaveLength(1);
    expect(fetchImpl.mock.calls.map(([url]) => url).sort()).toEqual([
      "http://b/api/agents/factory",
      "http://b/api/home/dashboard",
    ]);
  });

  it("never waits on the board", async () => {
    const { cache } = cacheWith({
      "/api/home/dashboard": () => jsonResponse(home),
      "/api/agents/factory": () => new Promise(() => {}),
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
      "/api/agents/factory": () => jsonResponse({}, false),
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
      "/api/agents/factory": () =>
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
      url.endsWith("/api/agents/factory")
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
});
