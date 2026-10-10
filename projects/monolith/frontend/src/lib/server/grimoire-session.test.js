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
      if (url.endsWith("/voices")) return response([]);
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
      if (url.endsWith("/voices"))
        return response([{ speaker_key: "ref:opaque" }]);
      if (url.endsWith("/sheets")) return response({ versions: [] });
      if (url.endsWith("/sessions")) return response([{ id: "session" }]);
      if (url.endsWith("/journal?view=party"))
        return response({
          rolls: [{ id: "party-roll", body: { total: 4 } }],
          surprise: "PARTY_EXTRA_FIELD",
        });
      if (url.endsWith("/sessions/session/journal"))
        return response({
          learned: [
            {
              event_id: "reveal",
              entity_id: "entity",
              entity: { name: "Visible" },
            },
          ],
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
    // Canonical API shape: the reusable JournalPanel reads it unchanged.
    expect(state.journal.mine).toEqual({
      learned: [
        {
          event_id: "reveal",
          entity_id: "entity",
          entity: { name: "Visible" },
        },
      ],
      received: [],
      people_and_places: [{ id: "entity", name: "Visible" }],
      rolls: [{ id: "roll", body: { total: 17 } }],
      open_threads: [],
      truncated: true,
    });
    expect(state.journal.party).toEqual({
      learned: [],
      received: [],
      people_and_places: [],
      rolls: [{ id: "party-roll", body: { total: 4 } }],
      open_threads: [],
      truncated: false,
    });
    expect(JSON.stringify(state.journal)).not.toContain("PARTY_EXTRA_FIELD");
    const journalUrls = fetch.mock.calls
      .map(([url]) => url)
      .filter((url) => url.includes("/journal"));
    expect(journalUrls.sort()).toEqual([
      expect.stringMatching(
        /\/campaigns\/campaign\/sessions\/session\/journal$/,
      ),
      expect.stringMatching(
        /\/campaigns\/campaign\/sessions\/session\/journal\?view=party$/,
      ),
    ]);
    expect(state.events).toHaveLength(501);
    expect(state.events.at(-1).seq).toBe(1003);
    expect(state).not.toHaveProperty("members");
    expect(state).not.toHaveProperty("npcs");
    expect(state.voices).toEqual([{ speaker_key: "ref:opaque" }]);
    expect(fetch.mock.calls.some(([url]) => url.includes("/entities"))).toBe(
      false,
    );
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
    expect(state.journal).toBeNull();
    expect(state.events).toEqual([]);
    expect(fetch).toHaveBeenCalledTimes(6);
  });

  it("paginates DM NPCs and filters non-NPC entities for the speaker picker", async () => {
    const fetch = vi.fn(async (url) => {
      if (url.endsWith("/lobby"))
        return response({ campaigns: [{ id: "campaign", role: "dm" }] });
      if (url.includes("/entities?")) {
        const query = new URL(url, "http://backend.test").searchParams;
        expect(query.get("type")).toBe("npc");
        expect(query.get("limit")).toBe("500");
        return response(
          query.has("cursor")
            ? {
                items: [{ id: "second", entity_type: "npc", name: "Mara" }],
                next_cursor: null,
              }
            : {
                items: [
                  { id: "first", entity_type: "npc", name: "Captain" },
                  { id: "place", entity_type: "location" },
                ],
                next_cursor: "500",
              },
        );
      }
      return response([]);
    });
    const state = await sessionState(fetch, cookies, "campaign");
    expect(state.npcs.map((npc) => npc.name)).toEqual(["Captain", "Mara"]);
    expect(fetch.mock.calls.some(([url]) => url.includes("cursor=500"))).toBe(
      true,
    );
  });
});
