import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

const document = {
  snapshotted_at: "2026-09-20T12:00:00Z",
  item: { id: 100123, title: "Public work item" },
  edges_in: [],
  edges_out: [],
};

describe("public factory work-item loader", () => {
  it("loads through the same-origin local snapshot proxy", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: { get: () => '"work-item"' },
      json: async () => document,
    });
    const setHeaders = vi.fn();
    const result = await load({ fetch, params: { id: "100123" }, setHeaders });

    expect(fetch).toHaveBeenCalledWith("/slop/factory/data/work-items/100123");
    expect(result.document).toEqual(document);
    expect(setHeaders).toHaveBeenCalled();
  });

  it("preserves missing versus unavailable outcomes", async () => {
    const missing = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    await expect(
      load({ fetch: missing, params: { id: "100123" }, setHeaders: vi.fn() }),
    ).rejects.toMatchObject({ status: 404 });

    const unavailable = vi.fn().mockResolvedValue({ ok: false, status: 503 });
    await expect(
      load({
        fetch: unavailable,
        params: { id: "100123" },
        setHeaders: vi.fn(),
      }),
    ).rejects.toMatchObject({ status: 503 });
  });
});
