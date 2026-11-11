import { describe, it, expect, vi } from "vitest";
import {
  createPublicSnapshotCache,
  PUBLIC_SNAPSHOTS,
} from "./public-snapshots.js";
const START = Date.parse("2026-10-24T12:00:00Z");
function response(ttl = 60, extra = {}) {
  return new Response('{"nodes":[],"edges":[]}', {
    headers: {
      "cache-control": `public, max-age=${ttl}, s-maxage=${ttl}, must-revalidate`,
      date: new Date(START).toUTCString(),
      etag: '"v1"',
      ...extra,
    },
  });
}
function fixture(fetcher = vi.fn(async () => response())) {
  let time = START;
  const cache = createPublicSnapshotCache({
    base: "http://public-backend",
    fetcher,
    now: () => time,
  });
  return {
    ...cache,
    fetcher,
    advance: (ms) => {
      time += ms;
    },
  };
}
describe("public snapshot cache", () => {
  it("does not repeatedly refresh a nonrenewable short lease", async () => {
    const cache = fixture(vi.fn(async () => response(13)));
    await cache.read("facts");
    for (let i = 0; i < 9; i++) {
      cache.advance(1000);
      await cache.read("facts");
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    expect(cache.fetcher).toHaveBeenCalledTimes(1);
    cache.advance(1000);
    await cache.read("facts");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(cache.fetcher).toHaveBeenCalledTimes(2);
    cache.advance(1000);
    await cache.read("facts");
    expect(cache.fetcher).toHaveBeenCalledTimes(2);
  });

  it("coalesces concurrent cold reads, and warm reads do no backend work", async () => {
    const cache = fixture();
    const cold = await Promise.all(
      Array.from({ length: 20 }, () => cache.read("graph")),
    );
    expect(cache.fetcher).toHaveBeenCalledTimes(1);
    cache.advance(1000);
    const warm = await cache.read("graph");
    expect(warm.body).toBe(cold[0].body);
    expect(warm.headers["cache-control"]).toContain("max-age=59");
    expect(warm.headers.etag).toBe('"testbuild-v1"');
    expect(cache.fetcher).toHaveBeenCalledTimes(1);
  });
  it("uses only allowlisted public projections, without credentials", async () => {
    const cache = fixture();
    await cache.prewarm();
    expect(cache.fetcher).toHaveBeenCalledTimes(6);
    for (const [url, options] of cache.fetcher.mock.calls) {
      expect(Object.values(PUBLIC_SNAPSHOTS)).toContain(
        url.replace("http://public-backend", ""),
      );
      expect(options).toMatchObject({
        credentials: "omit",
        redirect: "error",
        headers: { accept: "application/json" },
      });
    }
    await expect(cache.read("/api/knowledge/graph")).rejects.toThrow("unknown");
    await expect(cache.read("__proto__")).rejects.toThrow("unknown");
    expect(cache.fetcher).toHaveBeenCalledTimes(6);
  });
  it("refreshes before expiry without blocking valid warm readers", async () => {
    let release;
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(response())
      .mockImplementation(
        () =>
          new Promise((resolve) => {
            release = resolve;
          }),
      );
    const cache = fixture(fetcher);
    await cache.read("board");
    cache.advance(45000);
    const warm = await Promise.all([cache.read("board"), cache.read("board")]);
    expect(warm[0].headers["cache-control"]).toContain("max-age=15");
    expect(fetcher).toHaveBeenCalledTimes(2);
    release(
      response(60, {
        date: new Date(START + 45000).toUTCString(),
        etag: '"v2"',
      }),
    );
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect((await cache.read("board")).headers.etag).toBe('"testbuild-v2"');
  });
  it("never serves an expired snapshot after a refresh failure", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(response(10))
      .mockRejectedValue(new Error("offline"));
    const cache = fixture(fetcher);
    await cache.read("graph");
    cache.advance(10000);
    await expect(cache.read("graph")).rejects.toThrow("offline");
    await expect(cache.read("graph")).rejects.toThrow("backoff");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it.each(["no-store", ""])("does not retain %s responses", async (policy) => {
    const cache = fixture(
      vi.fn(async () => response(60, { "cache-control": policy })),
    );
    expect((await cache.read("facts")).headers["cache-control"]).toBe(
      "no-store",
    );
    await cache.read("facts");
    expect(cache.fetcher).toHaveBeenCalledTimes(2);
  });
  it("consumes origin age and caps a long origin lease at sixty seconds", async () => {
    const cache = fixture(vi.fn(async () => response(13, { age: "5" })));
    expect((await cache.read("graph")).headers["cache-control"]).toContain(
      "max-age=8",
    );
    cache.advance(2000);
    expect((await cache.read("graph")).headers["cache-control"]).toContain(
      "max-age=6",
    );
    const long = fixture(vi.fn(async () => response(3600)));
    await long.read("graph");
    long.advance(61000);
    await long.read("graph");
    expect(long.fetcher).toHaveBeenCalledTimes(2);
  });
  it("rejects redirects, cookies, non-JSON and backend errors without caching them", async () => {
    for (const bad of [
      new Response("login", { status: 302 }),
      response(60, { "cache-control": "private, max-age=60" }),
      response(13, { age: "13" }),
      new Response("{}", { headers: { "set-cookie": "session=x" } }),
      new Response("<html>"),
      new Response("oops", { status: 503 }),
    ]) {
      const cache = fixture(vi.fn(async () => bad));
      await expect(cache.read("graph")).rejects.toThrow();
      await expect(cache.read("graph")).rejects.toThrow("backoff");
    }
  });
  it("rejects a lease that expires while reading the body", async () => {
    let cache;
    const fetcher = vi.fn(async () => {
      const res = response(1);
      res.text = async () => {
        cache.advance(1000);
        return "{}";
      };
      return res;
    });
    cache = fixture(fetcher);
    await expect(cache.read("graph")).rejects.toThrow("expired");
  });
});
