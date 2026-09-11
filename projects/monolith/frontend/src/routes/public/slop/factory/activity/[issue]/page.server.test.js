import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

const payload = {
  snapshotted_at: "2026-09-11T14:32:00Z",
  policy: { generation: 7, max_tasks: 2 },
  task: { issue_number: 5980, title: "Worker-delivered probes", nodes: [] },
};

function ok() {
  return {
    ok: true,
    status: 200,
    headers: { get: () => '"task"' },
    json: async () => payload,
  };
}

describe("factory task loader", () => {
  it("loads one task through the same-origin proxy", async () => {
    const setHeaders = vi.fn();
    const fetch = vi.fn().mockResolvedValue(ok());

    const result = await load({ fetch, params: { issue: "5980" }, setHeaders });

    expect(fetch).toHaveBeenCalledWith("/slop/factory/data/tasks/5980");
    expect(result.task).toEqual(payload.task);
    expect(result.policy).toEqual(payload.policy);
    expect(result.snapshottedAt).toBe("2026-09-11T14:32:00Z");
    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "public, max-age=60, s-maxage=60",
      "cloudflare-cdn-cache-control": "public, max-age=60",
      etag: '"testbuild-task"',
    });
  });

  it("sends an unknown task to the 404 page", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 404 });

    await expect(
      load({ fetch, params: { issue: "1" }, setHeaders: vi.fn() }),
    ).rejects.toMatchObject({ status: 404 });
  });

  it("reports an upstream outage as 503", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 503 });

    await expect(
      load({ fetch, params: { issue: "1" }, setHeaders: vi.fn() }),
    ).rejects.toMatchObject({ status: 503 });
  });
});
