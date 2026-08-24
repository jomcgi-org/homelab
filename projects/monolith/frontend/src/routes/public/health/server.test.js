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

  it("returns degraded names on 200 without component details", async () => {
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        status: "ok",
        degraded: ["cd"],
        components: { cd: { ok: false, detail: "secret chart version" } },
      }),
    });

    const res = await GET({ fetch });
    const body = await res.json();
    expect(body).toEqual({ status: "ok", degraded: ["cd"] });
    expect(JSON.stringify(body)).not.toContain("components");
    expect(JSON.stringify(body)).not.toContain("detail");
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

  it("excludes advisory components from failing on 503", async () => {
    // Regression test for the bug: cd is advisory (in degraded array)
    // so it must not appear in failing.
    const fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        status: "unhealthy",
        components: {
          a: { ok: false, detail: "fatal" },
          cd: { ok: false, detail: "advisory chart lag" },
        },
        degraded: ["cd"],
      }),
    });

    const res = await GET({ fetch });

    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.failing).toEqual(["a"]);
    expect(body.degraded).toEqual(["cd"]);
    expect(JSON.stringify(body)).not.toContain("fatal");
    expect(JSON.stringify(body)).not.toContain("advisory chart lag");
  });

  it("omits failing key when only advisory components are not ok", async () => {
    // When all not-ok components are advisory, failing should not be present.
    const fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        status: "unhealthy",
        components: {
          cd: { ok: false, detail: "advisory chart lag" },
        },
        degraded: ["cd"],
      }),
    });

    const res = await GET({ fetch });

    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.failing).toBeUndefined();
    expect(body.degraded).toEqual(["cd"]);
  });

  it("handles 503 with no degraded key (older backend)", async () => {
    // Graceful fallback when backend does not include degraded array.
    const fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({
        status: "unhealthy",
        components: {
          a: { ok: false, detail: "fatal error" },
        },
      }),
    });

    const res = await GET({ fetch });

    expect(res.status).toBe(503);
    const body = await res.json();
    expect(body.failing).toEqual(["a"]);
    expect(body.degraded).toBeUndefined();
    expect(JSON.stringify(body)).not.toContain("fatal error");
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
