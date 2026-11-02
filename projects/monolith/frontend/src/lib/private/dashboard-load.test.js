import { describe, expect, it, vi } from "vitest";
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

describe("loadDashboard", () => {
  it("merges the home aggregate with a factory summary", async () => {
    const fetchMock = vi.fn(async (url) =>
      url.endsWith("/api/home/dashboard")
        ? jsonResponse(home)
        : jsonResponse(board),
    );
    const { dashboard } = await loadDashboard(fetchMock, "http://backend");
    expect(dashboard.health).toEqual(home.health);
    expect(dashboard.factory).toMatchObject({ state: "enabled", inFlight: 1 });
    expect(dashboard.factory.waiting).toHaveLength(1);
    expect(fetchMock.mock.calls.map(([url]) => url).sort()).toEqual([
      "http://backend/api/agents/factory",
      "http://backend/api/home/dashboard",
    ]);
  });

  it("keeps the home sections when the factory is down, and vice versa", async () => {
    const factoryDown = vi.fn(async (url) =>
      url.endsWith("/api/home/dashboard")
        ? jsonResponse(home)
        : jsonResponse({}, false),
    );
    expect((await loadDashboard(factoryDown, "http://b")).dashboard).toEqual({
      ...home,
      factory: null,
    });

    const homeDown = vi.fn(async (url) =>
      url.endsWith("/api/home/dashboard")
        ? Promise.reject(new Error("timeout"))
        : jsonResponse(board),
    );
    const { dashboard } = await loadDashboard(homeDown, "http://b");
    expect(dashboard.today).toBeUndefined();
    expect(dashboard.factory.state).toBe("enabled");
  });

  it("is null only when nothing answers", async () => {
    const down = vi.fn(async () => jsonResponse({}, false));
    expect(await loadDashboard(down, "http://b")).toEqual({ dashboard: null });
    const erroredBoth = vi.fn(async (url) =>
      url.endsWith("/api/home/dashboard")
        ? jsonResponse({ error: "broken" })
        : jsonResponse({}, false),
    );
    expect(await loadDashboard(erroredBoth, "http://b")).toEqual({
      dashboard: null,
    });
  });

  it("treats an aggregate that answers {error} as that one source down", async () => {
    const errored = vi.fn(async (url) =>
      url.endsWith("/api/home/dashboard")
        ? jsonResponse({ error: "broken" })
        : jsonResponse(board),
    );
    const { dashboard } = await loadDashboard(errored, "http://b");
    expect(dashboard.error).toBeUndefined();
    expect(dashboard.today).toBeUndefined();
    expect(dashboard.factory.waiting).toHaveLength(1);
  });
});
