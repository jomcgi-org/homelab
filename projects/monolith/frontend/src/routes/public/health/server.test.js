import { describe, it, expect, vi } from "vitest";
import { GET } from "./+server.js";
import { HEALTH_CACHE_CONTROL } from "../../../lib/cache-headers.js";

describe("/public/health GET", () => {
  it("proxies to the backend deep health endpoint", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok" }),
    });

    const res = await GET({ fetch });

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toMatch(/\/api\/health$/);
    expect(res.status).toBe(200);
  });

  it("caches only the healthy response (60s edge, no stale-if-error)", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: "ok" }),
    });

    const res = await GET({ fetch });

    expect(res.headers.get("cache-control")).toBe(HEALTH_CACHE_CONTROL);
    expect(HEALTH_CACHE_CONTROL).not.toContain("stale-if-error");
    expect(HEALTH_CACHE_CONTROL).not.toContain("stale-while-revalidate");
  });

  it("returns an uncached 503 when the backend reports unhealthy", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        status: "unhealthy",
        components: {
          b: { ok: true },
          a: { ok: false, detail: "secret" },
        },
      }),
    });

    const res = await GET({ fetch });

    expect(res.status).toBe(503);
    expect(res.headers.get("cache-control")).toBeNull();
    const body = await res.json();
    expect(body.failing).toEqual(["a"]);
    expect(JSON.stringify(body)).not.toContain("secret");
  });

  it("omits failing names when the backend body is not parseable", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => {
        throw new Error("not JSON");
      },
    });

    const res = await GET({ fetch });

    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({
      status: "unhealthy",
      backendStatus: 503,
    });
  });

  it("returns an uncached 503 when the backend is unreachable", async () => {
    const fetch = vi.fn().mockRejectedValue(new Error("connection refused"));

    const res = await GET({ fetch });

    expect(res.status).toBe(503);
    expect(res.headers.get("cache-control")).toBeNull();
  });
});
