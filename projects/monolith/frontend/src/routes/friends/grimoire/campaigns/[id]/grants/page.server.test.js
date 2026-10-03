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
    process.env.GRIMOIRE_PLAY_ENABLED = "true";
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response({ campaigns: [{ id: "campaign", role: "player" }] }),
      );
    await expect(
      load({ fetch, cookies, params: { id: "campaign" }, setHeaders() {} }),
    ).rejects.toMatchObject({ status: 403 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("loads every entity page with signed authority", async () => {
    process.env.GRIMOIRE_PLAY_ENABLED = "true";
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
    expect(result.entities.map((row) => row.id)).toEqual(["first", "last"]);
    for (const [, options] of fetch.mock.calls)
      expect(options.headers["x-grimoire-token"]).toBe("signed-token");
  });
});
