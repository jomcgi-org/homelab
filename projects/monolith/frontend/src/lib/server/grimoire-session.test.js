import { describe, expect, it, vi } from "vitest";
import { sessionState } from "./grimoire-session.js";

const cookies = { get: () => "signed-grimoire-token" };
const response = (value) => ({
  ok: true,
  status: 200,
  json: async () => value,
});

describe("sessionState", () => {
  it("loads the selected historical session and fails closed for an unknown session", async () => {
    const fetch = vi.fn(async (url) => {
      if (url.endsWith("/lobby"))
        return response({ campaigns: [{ id: "campaign", role: "player" }] });
      if (url.endsWith("/characters")) return response([]);
      if (url.endsWith("/sessions"))
        return response([{ id: "latest" }, { id: "old" }]);
      if (url.includes("/sessions/old/events")) return response([]);
      if (url.includes("/sessions/old/journal")) return response({});
      throw new Error(`Unexpected request ${url}`);
    });
    const state = await sessionState(fetch, cookies, "campaign", "old");
    expect(state.session.id).toBe("old");
    expect(state.selectedSessionId).toBe("old");
    expect(
      fetch.mock.calls.some(([url]) => url.includes("/sessions/latest/")),
    ).toBe(false);
    await expect(
      sessionState(fetch, cookies, "campaign", "../other"),
    ).rejects.toMatchObject({ status: 404 });
  });

  it("refuses a non-member before requesting campaign resources", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ campaigns: [] }));
    await expect(
      sessionState(fetch, cookies, "other-campaign"),
    ).rejects.toMatchObject({ status: 403 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("reads beyond the first page using the visible event sequence", async () => {
    const first = Array.from({ length: 500 }, (_, index) => ({
      id: String(index),
      seq: index * 2 + 1,
    }));
    const fetch = vi.fn(async (url) => {
      if (url.endsWith("/lobby"))
        return response({
          user: { id: "player" },
          campaigns: [{ id: "campaign", role: "player" }],
        });
      if (url.endsWith("/characters")) return response([{ id: "own-pc" }]);
      if (url.endsWith("/sheets")) return response({ versions: [] });
      if (url.endsWith("/sessions")) return response([{ id: "session" }]);
      if (url.includes("/journal"))
        return response({
          learned: [{ entity_id: "entity", entity: { name: "Visible" } }],
          people_and_places: [{ id: "entity", name: "Visible" }],
          rolls: [{ id: "roll", body: { total: 17 } }],
          truncated: true,
        });
      if (url.includes("after=0")) return response(first);
      if (url.includes("after=999"))
        return response([{ id: "last", seq: 1003 }]);
      throw new Error(`Unexpected request ${url}`);
    });
    const state = await sessionState(fetch, cookies, "campaign");
    expect(state.journal.mine.learned[0].projection.name).toBe("Visible");
    expect(state.journal.mine.people_places[0].entity_id).toBe("entity");
    expect(state.journal.mine.rolls[0]).toEqual({
      event_id: "roll",
      total: 17,
    });
    expect(state.journal.mine.truncated).toBe(true);
    expect(
      fetch.mock.calls.some(([url]) => url.endsWith("/journal?view=party")),
    ).toBe(true);
    expect(state.events).toHaveLength(501);
    expect(state.events.at(-1).seq).toBe(1003);
    expect(state).not.toHaveProperty("members");
    for (const [, options] of fetch.mock.calls) {
      expect(options.headers["x-grimoire-token"]).toBe("signed-grimoire-token");
    }
  });

  it("supports a campaign before its first session without requesting events", async () => {
    const fetch = vi.fn(async (url) => {
      if (url.endsWith("/lobby"))
        return response({ campaigns: [{ id: "campaign", role: "dm" }] });
      return response([]);
    });
    const state = await sessionState(fetch, cookies, "campaign");
    expect(state.session).toBeNull();
    expect(state.events).toEqual([]);
    expect(fetch).toHaveBeenCalledTimes(4);
  });
});
