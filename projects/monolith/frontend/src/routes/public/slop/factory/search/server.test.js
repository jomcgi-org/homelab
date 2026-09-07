import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const headers = { get: () => null };

describe("/public/slop/factory/search GET", () => {
  it("forwards bounded search parameters", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers,
      json: async () => [{ note_id: "fact" }],
    });
    const url = new URL(
      "https://jomcgi.dev/slop/factory/search?q=ember&mode=semantic&limit=5",
    );

    const response = await GET({ fetch, setHeaders: vi.fn(), url });

    const upstream = new URL(fetch.mock.calls[0][0]);
    expect(upstream.pathname).toBe("/api/knowledge/public/search");
    expect(upstream.search).toBe("?q=ember&mode=semantic&limit=5");
    expect(await response.json()).toEqual([{ note_id: "fact" }]);
  });

  it("preserves client errors and maps server errors to 503", async () => {
    const badRequest = vi.fn().mockResolvedValue({ ok: false, status: 422 });
    const unavailable = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    const url = new URL("https://jomcgi.dev/slop/factory/search?q=x");

    await expect(
      GET({ fetch: badRequest, setHeaders: vi.fn(), url }),
    ).rejects.toMatchObject({ status: 422 });
    await expect(
      GET({ fetch: unavailable, setHeaders: vi.fn(), url }),
    ).rejects.toMatchObject({ status: 503 });
  });
});
