import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const response = (body, status = 200) =>
  new Response(JSON.stringify(body), { status });
function event(fetch, query = "?q=clue", overrides = {}) {
  return {
    params: { id: campaignId },
    url: new URL(
      `https://friends.test/grimoire/campaigns/${campaignId}/knowledge/search${query}`,
    ),
    fetch,
    cookies: { get: () => "signed-token" },
    ...overrides,
  };
}
beforeEach(() => vi.stubEnv("API_BASE", "http://backend.test"));
afterEach(() => vi.unstubAllEnvs());

describe("knowledge search BFF", () => {
  it.each(["", "?q=", "?q=%20%20", `?q=${"x".repeat(201)}`])(
    "rejects an absent, blank or overlong query (%s)",
    async (query) => {
      const fetch = vi.fn();
      const result = await GET(event(fetch, query));
      expect(result.status).toBe(400);
      expect(result.headers.get("cache-control")).toBe("private, no-store");
      expect(await result.json()).toHaveProperty("error");
      expect(fetch).not.toHaveBeenCalled();
    },
  );
  it("rejects an invalid campaign UUID without a backend read", async () => {
    const fetch = vi.fn();
    const result = await GET(
      event(fetch, "?q=clue", { params: { id: "../other" } }),
    );
    expect(result.status).toBe(404);
    expect(result.headers.get("cache-control")).toBe("private, no-store");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("trims and encodes the query and proxies only q and k with the signed token", async () => {
    const results = [
      {
        type: "note",
        id: "note",
        preview: "Clue",
        source: { note_id: "note" },
      },
    ];
    const fetch = vi.fn().mockResolvedValue(response(results));
    const query = new URLSearchParams({
      q: "  clues & +/雪?  ",
      k: "7",
      as: "dm",
    });
    const result = await GET(event(fetch, `?${query}`));
    expect(fetch).toHaveBeenCalledWith(
      `http://backend.test/api/grimoire/campaigns/${campaignId}/knowledge/search?q=clues+%26+%2B%2F%E9%9B%AA%3F&k=7`,
      expect.objectContaining({
        headers: { "x-grimoire-token": "signed-token" },
      }),
    );
    expect(await result.json()).toEqual(results);
    expect(result.headers.get("cache-control")).toBe("private, no-store");
  });
  it.each([1, 200])(
    "accepts a query of %s characters and leaves k to the backend default",
    async (length) => {
      const q = "x".repeat(length);
      const fetch = vi.fn().mockResolvedValue(response([]));
      expect((await GET(event(fetch, `?q=${q}`))).status).toBe(200);
      expect(fetch.mock.calls[0][0]).toBe(
        `http://backend.test/api/grimoire/campaigns/${campaignId}/knowledge/search?q=${q}`,
      );
    },
  );
  it.each([403, 404, 422, 500, 503])(
    "propagates backend status %s and its error without inventing results",
    async (status) => {
      const fetch = vi
        .fn()
        .mockResolvedValue(response({ detail: "Search unavailable" }, status));
      const result = await GET(event(fetch));
      expect(result.status).toBe(status);
      expect(await result.json()).toEqual({ error: "Search unavailable" });
      expect(result.headers.get("cache-control")).toBe("private, no-store");
    },
  );
  it("fails closed on network errors and missing credentials", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("Network unavailable"));
    const result = await GET(event(fetch));
    expect(result.status).toBe(502);
    expect(await result.json()).toEqual({ error: "Network unavailable" });
    expect(result.headers.get("cache-control")).toBe("private, no-store");
    fetch.mockClear();
    const expired = await GET(
      event(fetch, "?q=clue", { cookies: { get: () => undefined } }),
    );
    expect(expired.status).toBe(502);
    expect(await expired.json()).toHaveProperty("error");
    expect(fetch).not.toHaveBeenCalled();
  });
  it("preserves a non-JSON upstream error as an error response", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response("Unavailable", { status: 503 }));
    const result = await GET(event(fetch));
    expect(result.status).toBe(503);
    expect(await result.json()).toEqual({
      error: "Grimoire could not complete that request.",
    });
  });
});
