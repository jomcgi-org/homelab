import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const headers = { get: () => null };
const task = { snapshotted_at: "2026-09-11T14:32:00Z", policy: {}, task: {} };

describe("/public/slop/factory/data/tasks/[issue] GET", () => {
  it("proxies the issue number to the factory task endpoint", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue({ ok: true, headers, json: async () => task });

    const response = await GET({
      fetch,
      params: { issue: "5980" },
      setHeaders: vi.fn(),
    });

    expect(fetch.mock.calls[0][0]).toMatch(
      /\/api\/agents\/public\/factory\/tasks\/5980$/,
    );
    expect(await response.json()).toEqual(task);
  });

  it.each(["abc", "59 80", "../secrets", "5980a", ""])(
    "refuses %s without calling the backend",
    async (issue) => {
      const fetch = vi.fn();

      await expect(
        GET({ fetch, params: { issue }, setHeaders: vi.fn() }),
      ).rejects.toMatchObject({ status: 404 });
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("keeps an unknown task as a 404 and other failures as 503", async () => {
    const missing = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    await expect(
      GET({ fetch: missing, params: { issue: "1" }, setHeaders: vi.fn() }),
    ).rejects.toMatchObject({ status: 404 });

    const broken = vi.fn().mockResolvedValue({ ok: false, status: 502 });
    await expect(
      GET({ fetch: broken, params: { issue: "1" }, setHeaders: vi.fn() }),
    ).rejects.toMatchObject({ status: 503 });
  });
});
