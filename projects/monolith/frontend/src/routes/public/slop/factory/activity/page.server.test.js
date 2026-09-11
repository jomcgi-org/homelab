import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

const board = {
  snapshotted_at: "2026-09-11T14:32:00Z",
  state: "enabled",
  policy: { generation: 7, max_tasks: 2 },
  active: [{ issue_number: 5980 }],
  queued: [],
  recent: [],
};

describe("factory activity loader", () => {
  it("loads the board through the same-origin proxy", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => '"board"' },
      json: async () => board,
    });

    const result = await load({ fetch, setHeaders });

    expect(fetch).toHaveBeenCalledWith("/slop/factory/data/board");
    expect(result.board).toEqual(board);
    expect(result.unavailable).toBe(false);
    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "public, max-age=60, s-maxage=60",
      "cloudflare-cdn-cache-control": "public, max-age=60",
      etag: '"testbuild-board"',
    });
  });

  it("keeps rendering with an empty board when the proxy fails", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 503 });

    const result = await load({ fetch, setHeaders });

    expect(result.unavailable).toBe(true);
    expect(result.board).toMatchObject({ active: [], queued: [], recent: [] });
    expect(result.title).toBe("Factory activity");
    expect(setHeaders.mock.calls[0][0].etag).toBeUndefined();
  });
});
