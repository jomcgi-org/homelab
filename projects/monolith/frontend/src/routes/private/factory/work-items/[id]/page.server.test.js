import { describe, it, expect, vi, beforeEach } from "vitest";
import { load } from "./+page.server.js";

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

    const result = await load({ params: { id: "42" } });

    expect(result.itemId).toBe("42");
    expect(result.document).toEqual(mockDoc);
    expect(result.missing).toBeUndefined();
    expect(result.error).toBeUndefined();
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
