import { describe, it, expect, vi, beforeEach } from "vitest";

vi.hoisted(() => {
  process.env.API_BASE = "http://backend";
});

import { load } from "./+page.server.js";
import { GET } from "./+server.js";

describe("work item page loader", () => {
  beforeEach(() => {
    global.fetch = vi.fn();
  });

  it("loads a work item successfully", async () => {
    const mockDoc = {
      item: { id: 42, title: "Test issue", state: "open" },
      edges_in: [],
      edges_out: [],
      events: [],
    };

    global.fetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => mockDoc,
    });

    const result = await load({
      params: { id: "42" },
      request: new Request("https://private.jomcgi.dev/factory/work-items/42", {
        headers: {
          "x-auth-email": "viewer@example.test",
          "cf-access-authenticated-user-email": "forged@example.test",
        },
      }),
    });

    expect(result.itemId).toBe("42");
    expect(result.document).toEqual(mockDoc);
    expect(result.missing).toBeUndefined();
    expect(result.error).toBeUndefined();
    expect(global.fetch).toHaveBeenCalledWith(
      "http://backend/api/agents/factory/work-items/42",
      {
        headers: { "X-Auth-Email": "viewer@example.test" },
        signal: expect.any(AbortSignal),
      },
    );
  });

  it("returns missing=true for 404", async () => {
    global.fetch.mockResolvedValueOnce({
      ok: false,
      status: 404,
    });

    const result = await load({ params: { id: "999" } });

    expect(result.itemId).toBe("999");
    expect(result.missing).toBe(true);
  });

  it("forwards only the verified identity when polling the work item", async () => {
    const document = { item: { id: 42, title: "Test issue", state: "open" } };
    global.fetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => document,
    });

    const response = await GET({
      params: { id: "42" },
      request: new Request("https://private.jomcgi.dev/factory/work-items/42", {
        headers: {
          "x-auth-email": "viewer@example.test",
          "cf-access-authenticated-user-email": "forged@example.test",
        },
      }),
    });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(document);
    expect(global.fetch).toHaveBeenCalledWith(
      "http://backend/api/agents/factory/work-items/42",
      {
        headers: { "X-Auth-Email": "viewer@example.test" },
        signal: expect.any(AbortSignal),
      },
    );
  });

  it("returns error=true on fetch failure", async () => {
    global.fetch.mockRejectedValueOnce(new Error("network error"));

    const result = await load({ params: { id: "42" } });

    expect(result.itemId).toBe("42");
    expect(result.error).toBe(true);
  });

  it("returns error=true on non-OK response", async () => {
    global.fetch.mockResolvedValueOnce({
      ok: false,
      status: 502,
    });

    const result = await load({ params: { id: "42" } });

    expect(result.itemId).toBe("42");
    expect(result.error).toBe(true);
  });

  it("returns missing=true for invalid id", async () => {
    const result = await load({ params: { id: "not-a-number" } });

    expect(result.itemId).toBe("not-a-number");
    expect(result.missing).toBe(true);
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
