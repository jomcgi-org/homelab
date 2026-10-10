import { beforeEach, describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";
import { GET } from "./state/+server.js";

const cookies = { get: () => "signed-grimoire-token" };
const response = (value) => ({
  ok: true,
  status: 200,
  json: async () => value,
});
const session = {
  id: "33333333-3333-4333-8333-333333333333",
  status: "active",
};
const members = [
  { id: "member-dm", role: "dm", player_character_id: null },
  { id: "member-a", role: "player", player_character_id: "pc-a" },
];

// A backend that answers the same campaign for either role. The player
// variant must never be asked for the member roster.
function backend(role) {
  return vi.fn(async (url) => {
    if (url.endsWith("/lobby"))
      return response({
        user: { id: "viewer" },
        campaigns: [{ id: "campaign", name: "Adventure", role }],
      });
    if (url.endsWith("/characters"))
      return response([{ id: "pc-a", character_name: "Aria" }]);
    if (url.endsWith("/sheets")) return response({ versions: [] });
    if (url.endsWith("/sessions")) return response([session]);
    if (url.includes("/events")) return response([]);
    if (url.includes("/journal")) return response({});
    if (url.endsWith("/members")) return response(members);
    throw new Error(`Unexpected request ${url}`);
  });
}

async function loadAs(role) {
  const fetch = backend(role);
  const setHeaders = vi.fn();
  const data = await load({
    fetch,
    cookies,
    params: { id: "campaign" },
    setHeaders,
  });
  return { data, fetch, setHeaders };
}

// Names a DM-only control or composer concern could travel under. None is a
// key the page data carries for a player.
const dmOnlyKeys = [
  "members",
  "audience",
  "audiences",
  "controls",
  "actions",
  "composer",
  "narration",
];

describe("session page load", () => {
  beforeEach(() => {
    process.env.API_BASE = "http://backend.test";
    process.env.GRIMOIRE_PLAY_ENABLED = "true";
  });

  it("keeps a search-linked session in both the initial load and the state BFF", async () => {
    const original = backend("player");
    const historical = {
      id: "44444444-4444-4444-8444-444444444444",
      status: "ended",
    };
    const fetch = vi.fn(async (url, options) =>
      url.endsWith("/sessions")
        ? response([session, historical])
        : original(url, options),
    );
    const request = {
      fetch,
      cookies,
      params: { id: "campaign" },
      setHeaders: vi.fn(),
      url: new URL(
        `https://friends.test/grimoire/campaigns/campaign/session?session=${historical.id}`,
      ),
    };
    const data = await load(request);
    expect(data.session).toEqual(historical);
    expect(data.selectedSessionId).toBe(historical.id);
    const refreshed = await GET(request);
    expect(refreshed.status).toBe(200);
    expect((await refreshed.json()).session).toEqual(historical);
    expect(refreshed.headers.get("cache-control")).toBe("private, no-store");
    expect(
      fetch.mock.calls
        .filter(([url]) => url.includes("/events"))
        .every(([url]) => url.includes(historical.id)),
    ).toBe(true);
  });

  it("is unavailable while play is disabled", async () => {
    delete process.env.GRIMOIRE_PLAY_ENABLED;
    const fetch = backend("dm");
    await expect(
      load({ fetch, cookies, params: { id: "campaign" }, setHeaders: vi.fn() }),
    ).rejects.toMatchObject({ status: 404 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("gives a player no DM-only controls or audience roster", async () => {
    const { data, fetch, setHeaders } = await loadAs("player");
    expect(data.campaign.role).toBe("player");
    expect(data.session).toEqual(session);
    for (const key of dmOnlyKeys) expect(data).not.toHaveProperty(key);
    // The roster is the only DM-only data. The player load must not even
    // request it, so a backend bug could not leak it through the BFF.
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/members"))).toBe(
      false,
    );
    const serialized = JSON.stringify(data);
    expect(serialized).not.toContain("member-dm");
    expect(serialized).not.toContain("member-a");
    // Lifecycle verbs are not state the page receives: the DM view derives
    // them from its role, so nothing in player data names one.
    expect(serialized).not.toMatch(/"(start|pause|resume|end)"/);
    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "private, no-store",
    });
  });

  it("gives the DM the roster the audience picker and private replies need", async () => {
    const { data, fetch } = await loadAs("dm");
    expect(data.campaign.role).toBe("dm");
    expect(data.members).toEqual(members);
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/members"))).toBe(
      true,
    );
  });
});
