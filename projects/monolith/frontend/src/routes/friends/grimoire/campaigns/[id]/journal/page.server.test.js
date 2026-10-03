import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import Page from "./+page.svelte";
import { load } from "./+page.server.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const otherId = "22222222-2222-4222-8222-222222222222";
const membership = { id: campaignId, name: "Our campaign", role: "player" };
const journal = { sessions: [], next_cursor: null };
const response = (body, status = 200) => ({
  ok: status < 400,
  status,
  json: async () => body,
});
function event(fetch, query = "", overrides = {}) {
  return {
    params: { id: campaignId },
    url: new URL(
      `https://friends.jomcgi.dev/grimoire/campaigns/${campaignId}/journal${query}`,
    ),
    fetch,
    cookies: {
      get: (key) => (key === "grimoire-id-token" ? "signed-token" : undefined),
    },
    setHeaders: vi.fn(),
    ...overrides,
  };
}
function reads(body = journal) {
  return vi
    .fn()
    .mockResolvedValueOnce(response({ campaigns: [membership] }))
    .mockResolvedValueOnce(response(body));
}

beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
});

describe("journal load", () => {
  it("forwards view and opaque cursor, verifies membership, and disables caching", async () => {
    const fetch = reads();
    const request = event(fetch, "?view=party&cursor=next%2B%2F%3D%26");
    expect(await load(request)).toEqual({
      campaign: membership,
      journal,
      view: "party",
    });
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "http://backend.test/api/grimoire/lobby",
      `http://backend.test/api/grimoire/campaigns/${campaignId}/journal?view=party&cursor=next%2B%2F%3D%26`,
    ]);
    expect(request.setHeaders).toHaveBeenCalledWith({
      "cache-control": "private, no-store",
    });
    for (const [, options] of fetch.mock.calls)
      expect(options.headers).toEqual({ "x-grimoire-token": "signed-token" });
  });

  it.each(["", "?view=invalid"])(
    "defaults to mine without inventing a cursor (%s)",
    async (query) => {
      const fetch = reads();
      expect((await load(event(fetch, query))).view).toBe("mine");
      expect(fetch.mock.calls[1][0]).toBe(
        `http://backend.test/api/grimoire/campaigns/${campaignId}/journal?view=mine`,
      );
    },
  );

  it("returns 404 for an invalid UUID before fetching", async () => {
    const fetch = vi.fn();
    await expect(
      load(event(fetch, "", { params: { id: "bad" } })),
    ).rejects.toMatchObject({ status: 404 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("returns 404 for a non-member without reading the journal", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response({ campaigns: [{ ...membership, id: otherId }] }),
      );
    await expect(load(event(fetch))).rejects.toMatchObject({ status: 404 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([404, 403, 500])(
    "rejects a failed journal read (%s), including disabled play",
    async (status) => {
      const fetch = vi
        .fn()
        .mockResolvedValueOnce(response({ campaigns: [membership] }))
        .mockResolvedValueOnce(
          response({ detail: "Journal unavailable" }, status),
        );
      await expect(load(event(fetch))).rejects.toThrow("Journal unavailable");
      expect(fetch).toHaveBeenCalledTimes(2);
    },
  );

  it("rejects network errors and failed lobby reads", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ campaigns: [membership] }))
      .mockRejectedValueOnce(new Error("Network unavailable"));
    await expect(load(event(fetch))).rejects.toThrow("Network unavailable");
    const lobby = vi
      .fn()
      .mockResolvedValue(response({ detail: "Lobby unavailable" }, 500));
    await expect(load(event(lobby))).rejects.toThrow("Lobby unavailable");
    expect(lobby).toHaveBeenCalledTimes(1);
  });
});

describe("journal page", () => {
  it("renders session journals, preserves view in pagination, and resets the cursor on view changes", () => {
    const { body } = render(Page, {
      props: {
        data: {
          campaign: membership,
          view: "party",
          journal: {
            next_cursor: "opaque+/=&",
            sessions: [
              {
                session_id: otherId,
                started_at: "2026-10-03T08:00:00Z",
                journal: {
                  learned: [],
                  received: [{ id: "handout", body: { text: "Party map" } }],
                  people_and_places: [],
                  rolls: [],
                  open_threads: [],
                },
              },
            ],
          },
        },
      },
    });
    expect(body).toContain("Our campaign journal");
    expect(body).toContain("Party map");
    expect(body).toContain('datetime="2026-10-03T08:00:00Z"');
    expect(body).toContain('href="?view=party&amp;cursor=opaque%2B%2F%3D%26"');
    expect(body).toContain('value="party" aria-pressed="true"');
    expect(body.match(/aria-label="Journal audience"/g)).toHaveLength(1);
    expect(body).not.toContain('name="cursor"');
  });

  it("renders an empty campaign without a pagination link", () => {
    const { body } = render(Page, {
      props: { data: { campaign: membership, journal, view: "mine" } },
    });
    expect(body).toContain("No sessions recorded yet.");
    expect(body).not.toContain("Older sessions");
  });
});
