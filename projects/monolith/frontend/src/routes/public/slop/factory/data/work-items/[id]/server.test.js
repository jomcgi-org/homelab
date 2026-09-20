import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const document = {
  snapshotted_at: "2026-09-20T12:00:00Z",
  item: { id: 100123, title: "Public work item" },
  edges_in: [],
  edges_out: [],
};

describe("public factory work-item proxy", () => {
  it("reads the local public API by work-item identity", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => '"work-item"' },
      json: async () => document,
    });
    const response = await GET({
      fetch,
      params: { id: "100123" },
      setHeaders: vi.fn(),
    });

    expect(fetch.mock.calls[0][0]).toMatch(
      /\/api\/agents\/public\/factory\/work-items\/100123$/,
    );
    expect(fetch.mock.calls[0][0]).not.toContain("github.com");
    expect(await response.json()).toEqual(document);
  });

  it.each(["6257", "abc", "100 123", "../private", "100123x", ""])(
    "rejects non-work-item identity %s without a backend read",
    async (id) => {
      const fetch = vi.fn();
      await expect(
        GET({ fetch, params: { id }, setHeaders: vi.fn() }),
      ).rejects.toMatchObject({ status: 404 });
      expect(fetch).not.toHaveBeenCalled();
    },
  );
});
