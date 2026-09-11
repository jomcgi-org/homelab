import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const headers = { get: () => null };
const session = {
  snapshotted_at: "2026-09-11T14:32:00Z",
  session: { key: "factory:5980:implement:1" },
  turns: [],
};

describe("/public/slop/factory/data/sessions/[...key] GET", () => {
  it("encodes the whole key into one upstream path segment", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, headers, json: async () => session });

    const response = await GET({
      fetch,
      params: { key: "factory:5980:implement:1" },
      setHeaders: vi.fn(),
    });

    expect(fetch.mock.calls[0][0]).toMatch(
      /\/api\/agents\/public\/factory\/sessions\/factory%3A5980%3Aimplement%3A1$/,
    );
    expect(await response.json()).toEqual(session);
  });

  it("keeps a node key that carries its own colons intact", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, headers, json: async () => session });

    await GET({
      fetch,
      params: { key: "factory:5980:verify:delivery:2" },
      setHeaders: vi.fn(),
    });

    expect(fetch.mock.calls[0][0]).toContain(
      "sessions/factory%3A5980%3Averify%3Adelivery%3A2",
    );
  });

  it("refuses an empty key without calling the backend", async () => {
    const fetch = vi.fn();

    await expect(
      GET({ fetch, params: { key: "" }, setHeaders: vi.fn() }),
    ).rejects.toMatchObject({ status: 404 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("keeps an unknown session as a 404 and other failures as 503", async () => {
    const missing = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    await expect(
      GET({
        fetch: missing,
        params: { key: "factory:1:plan:1" },
        setHeaders: vi.fn(),
      }),
    ).rejects.toMatchObject({ status: 404 });

    const broken = vi.fn().mockResolvedValue({ ok: false, status: 500 });
    await expect(
      GET({
        fetch: broken,
        params: { key: "factory:1:plan:1" },
        setHeaders: vi.fn(),
      }),
    ).rejects.toMatchObject({ status: 503 });
  });
});
