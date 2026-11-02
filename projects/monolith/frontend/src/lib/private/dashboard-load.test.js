import { afterEach, describe, expect, it, vi } from "vitest";
import {
  BOARD_FRESH_MS,
  BOARD_STALE_MS,
  factorySummary,
  loadDashboard,
  resetBoardCache,
} from "./dashboard-load.js";

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

afterEach(() => resetBoardCache());

const homeFetch = (body = home, ok = true) =>
  vi.fn(async () => jsonResponse(body, ok));

describe("loadDashboard", () => {
  it("answers without waiting for the board, then serves the cached summary", async () => {
    let finish;
    const boardFetch = vi.fn(
      () =>
        new Promise((resolve) => {
          finish = () => resolve(jsonResponse(board));
        }),
    );
    const first = await loadDashboard(homeFetch(), "http://b", { boardFetch });
    expect(first.dashboard.health).toEqual(home.health);
    expect(first.dashboard.factory).toBeNull();
    expect(boardFetch).toHaveBeenCalledWith(
      "http://b/api/agents/factory",
      expect.anything(),
    );
    finish();
    await new Promise((r) => setTimeout(r, 0));
    const second = await loadDashboard(homeFetch(), "http://b", { boardFetch });
    expect(second.dashboard.factory).toMatchObject({
      state: "enabled",
      inFlight: 1,
    });
    expect(second.dashboard.factory.waiting).toHaveLength(1);
    expect(boardFetch).toHaveBeenCalledTimes(1);
  });

  it("keeps the home sections when the board has no reading, and vice versa", async () => {
    const boardDown = vi.fn(async () => jsonResponse({}, false));
    const { dashboard } = await loadDashboard(homeFetch(), "http://b", {
      boardFetch: boardDown,
    });
    expect(dashboard).toEqual({ ...home, factory: null });

    resetBoardCache();
    let t = 0;
    const now = () => t;
    const boardUp = vi.fn(async () => jsonResponse(board));
    factorySummary(boardUp, "http://b", now);
    await new Promise((r) => setTimeout(r, 0));
    const homeDown = vi.fn(async () => Promise.reject(new Error("timeout")));
    const result = await loadDashboard(homeDown, "http://b", {
      boardFetch: boardUp,
      now,
    });
    expect(result.dashboard.today).toBeUndefined();
    expect(result.dashboard.factory.state).toBe("enabled");
  });

  it("is null only when nothing answers, and an {error} aggregate counts as down", async () => {
    const boardDown = vi.fn(async () => jsonResponse({}, false));
    expect(
      await loadDashboard(homeFetch({}, false), "http://b", {
        boardFetch: boardDown,
      }),
    ).toEqual({ dashboard: null });
    expect(
      await loadDashboard(homeFetch({ error: "broken" }), "http://b", {
        boardFetch: boardDown,
      }),
    ).toEqual({ dashboard: null });
  });
});

describe("factorySummary", () => {
  it("reads the board at most once while a read is in flight", () => {
    const boardFetch = vi.fn(() => new Promise(() => {}));
    for (let i = 0; i < 5; i++) factorySummary(boardFetch, "http://b");
    expect(boardFetch).toHaveBeenCalledTimes(1);
  });

  it("refreshes after a minute and drops a summary older than five", async () => {
    let t = 0;
    const now = () => t;
    const boardFetch = vi.fn(async () => jsonResponse(board));
    factorySummary(boardFetch, "http://b", now);
    await new Promise((r) => setTimeout(r, 0));
    expect(factorySummary(boardFetch, "http://b", now)).not.toBeNull();
    expect(boardFetch).toHaveBeenCalledTimes(1);

    t = BOARD_FRESH_MS;
    const failing = vi.fn(async () => jsonResponse({}, false));
    expect(factorySummary(failing, "http://b", now)).not.toBeNull();
    expect(failing).toHaveBeenCalledTimes(1);
    await new Promise((r) => setTimeout(r, 0));

    t = BOARD_STALE_MS;
    expect(factorySummary(failing, "http://b", now)).toBeNull();
  });

  it("spaces attempts after a failed read", async () => {
    let t = 0;
    const now = () => t;
    const failing = vi.fn(async () => jsonResponse({}, false));
    factorySummary(failing, "http://b", now);
    await new Promise((r) => setTimeout(r, 0));
    t = BOARD_FRESH_MS - 1;
    factorySummary(failing, "http://b", now);
    expect(failing).toHaveBeenCalledTimes(1);
    t = BOARD_FRESH_MS;
    factorySummary(failing, "http://b", now);
    expect(failing).toHaveBeenCalledTimes(2);
  });

  it("swallows a malformed board instead of rejecting", async () => {
    const malformed = vi.fn(async () =>
      jsonResponse({ ok: true, active: 5, escalations: "x" }),
    );
    const rejections = [];
    const onReject = (reason) => rejections.push(reason);
    process.on("unhandledRejection", onReject);
    try {
      factorySummary(malformed, "http://b");
      await new Promise((r) => setTimeout(r, 10));
    } finally {
      process.off("unhandledRejection", onReject);
    }
    expect(rejections).toEqual([]);
  });
});
