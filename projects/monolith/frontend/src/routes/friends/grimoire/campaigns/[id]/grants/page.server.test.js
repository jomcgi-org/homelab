import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";
const cookies = { get: () => "signed-token" };
const response = (value) => ({
  ok: true,
  status: 200,
  json: async () => value,
});

describe("DM grants table", () => {
  it("rejects a player before requesting any DM resource", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response({ campaigns: [{ id: "campaign", role: "player" }] }),
      );
    await expect(
      load({ fetch, cookies, params: { id: "campaign" }, setHeaders() {} }),
    ).rejects.toMatchObject({ status: 403 });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toMatch(/\/lobby$/);
  });
  it("loads one bounded granted page without draining corpus pagination", async () => {
    const fetch = vi.fn(async (url) => {
      if (url.endsWith("/lobby"))
        return response({ campaigns: [{ id: "campaign", role: "dm" }] });
      if (url.includes("cursor=500"))
        return response({ items: [{ id: "last" }], next_cursor: null });
      if (url.includes("/entities"))
        return response({ items: [{ id: "first" }], next_cursor: "500" });
      return response([]);
    });
    const result = await load({
      fetch,
      cookies,
      params: { id: "campaign" },
      setHeaders() {},
    });
    expect(result.entities.map((row) => row.id)).toEqual(["first"]);
    expect(result.nextCursor).toBe("500");
    expect(
      fetch.mock.calls.filter(([url]) => url.includes("/entities")),
    ).toHaveLength(1);
    expect(
      fetch.mock.calls.some(([url]) =>
        url.includes("granted_only=true&limit=100"),
      ),
    ).toBe(true);
    // The reveal panel needs the campaign and the recipient characters.
    expect(result.campaign.id).toBe("campaign");
    expect(result.characters).toEqual([]);
    for (const [, options] of fetch.mock.calls)
      expect(options.headers["x-grimoire-token"]).toBe("signed-token");
  });
  it("searches on the server and retains granted entities outside the search", async () => {
    const fetch = vi.fn(async (url) => {
      if (url.endsWith("/lobby"))
        return response({ campaigns: [{ id: "campaign", role: "dm" }] });
      if (url.includes("granted_only"))
        return response({ items: [{ id: "known" }], next_cursor: null });
      if (url.includes("/entities?limit=50&q=Hidden"))
        return response({ items: [{ id: "new" }], next_cursor: "50" });
      return response([]);
    });
    const result = await load({
      fetch,
      cookies,
      params: { id: "campaign" },
      setHeaders() {},
      url: new URL("https://example.test/?q=Hidden"),
    });
    expect(result.entities.map((row) => row.id)).toEqual(["known", "new"]);
    expect(result.searchTruncated).toBe(true);
  });
  it.each(["?cursor=-1", "?cursor=nope", `?q=${"a".repeat(121)}`])(
    "rejects invalid query parameters %s",
    async (query) => {
      const fetch = vi.fn(async (url) =>
        url.endsWith("/lobby")
          ? response({ campaigns: [{ id: "campaign", role: "dm" }] })
          : response([]),
      );
      await expect(
        load({
          fetch,
          cookies,
          params: { id: "campaign" },
          setHeaders() {},
          url: new URL(`https://example.test/${query}`),
        }),
      ).rejects.toMatchObject({ status: 400 });
      expect(
        fetch.mock.calls.every(([url]) => !url.includes("/entities")),
      ).toBe(true);
    },
  );
});
