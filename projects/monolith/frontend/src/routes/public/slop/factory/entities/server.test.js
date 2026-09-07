import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const makeHeaders = (values = {}) => ({
  get(name) {
    return values[name.toLowerCase()] ?? null;
  },
});

describe("/public/slop/factory/entities GET", () => {
  it("proxies the public entity catalog unchanged", async () => {
    const entities = [{ kind: "project", slug: "embervm" }];
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders(),
      json: async () => entities,
    });

    const response = await GET({ fetch, setHeaders: vi.fn() });

    expect(fetch.mock.calls[0][0]).toMatch(
      /\/api\/knowledge\/public\/entities$/,
    );
    expect(await response.json()).toEqual(entities);
  });

  it("versions upstream validators and preserves last-modified", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: makeHeaders({ etag: '"entities"', "last-modified": "today" }),
      json: async () => [],
    });

    await GET({ fetch, setHeaders });

    expect(setHeaders.mock.calls[0][0].etag).toBe('"testbuild-entities"');
    expect(setHeaders.mock.calls[0][0]["last-modified"]).toBe("today");
  });

  it("returns 503 when the backend fails", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false });
    await expect(GET({ fetch, setHeaders: vi.fn() })).rejects.toThrow();
  });
});
