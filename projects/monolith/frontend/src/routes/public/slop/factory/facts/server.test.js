import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const makeHeaders = (values = {}) => ({
  get(name) {
    return values[name.toLowerCase()] ?? null;
  },
});

describe("/public/slop/factory/facts GET", () => {
  it("proxies the public daily fact figures", async () => {
    const payload = {
      daily: [{ d: "2026-09-07", verified: 2, unverified: 1 }],
      totals: { verified: 2, unverified: 1, disputed: 0 },
      contradictions: 1,
    };
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({ etag: '"facts"', "last-modified": "today" }),
      json: async () => payload,
    });

    const response = await GET({ fetch, setHeaders });

    expect(fetch.mock.calls[0][0]).toMatch(
      /\/api\/knowledge\/public\/facts\/daily$/,
    );
    expect(await response.json()).toEqual(payload);
    expect(setHeaders.mock.calls[0][0].etag).toBe('"testbuild-facts"');
    expect(setHeaders.mock.calls[0][0]["last-modified"]).toBe("today");
  });

  it("maps an upstream failure to 503", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false });

    await expect(GET({ fetch, setHeaders: vi.fn() })).rejects.toMatchObject({
      status: 503,
    });
  });
});
