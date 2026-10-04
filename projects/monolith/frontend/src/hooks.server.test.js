import { describe, expect, it } from "vitest";
import { gunzipSync } from "node:zlib";
import { handle, pickEncoding } from "./hooks.server.js";

describe("response compression", () => {
  it.each([
    [null, null],
    ["identity", null],
    ["gzip;q=1, br;q=0", "gzip"],
    ["gzip;q=0, identity;q=1", null],
    ["gzip;q=1, br;q=0.1", "gzip"],
    ["gzip, br", "br"],
    ["BR; q=0.8, gzip;q=0.5", "br"],
    ["*;q=0.5, br;q=0", "gzip"],
    ["*;q=0", null],
    ["gzip;q=0.5, identity;q=1", null],
    ["gzip;q=invalid", null],
    ["gzip;q=2", null],
  ])("negotiates %s as %s", (header, expected) => {
    expect(pickEncoding(header)).toBe(expected);
  });
  it("returns decodable gzip when Brotli is rejected", async () => {
    const body = "response ".repeat(300);
    const response = await handle({
      event: {
        request: new Request("https://example.com", {
          headers: { "Accept-Encoding": "gzip;q=1, br;q=0" },
        }),
      },
      resolve: async () =>
        new Response(body, { headers: { "Content-Type": "text/plain" } }),
    });
    expect(response.headers.get("content-encoding")).toBe("gzip");
    expect(response.headers.get("vary")).toContain("Accept-Encoding");
    expect(
      gunzipSync(Buffer.from(await response.arrayBuffer())).toString(),
    ).toBe(body);
  });
  it("leaves the body readable when compression is rejected", async () => {
    const body = "response ".repeat(300);
    const response = await handle({
      event: {
        request: new Request("https://example.com", {
          headers: { "Accept-Encoding": "gzip;q=0, br;q=0" },
        }),
      },
      resolve: async () =>
        new Response(body, { headers: { "Content-Type": "text/plain" } }),
    });
    expect(response.headers.has("content-encoding")).toBe(false);
    expect(await response.text()).toBe(body);
  });
});

describe("Activity index cache isolation", () => {
  const route = "/public/slop/factory/activity";
  const publicHeaders = {
    "cache-control": "public, max-age=300, s-maxage=300",
    "cloudflare-cdn-cache-control": "public, max-age=300",
    etag: '"old-activity"',
    vary: "Accept",
  };

  async function respond({
    id = route,
    path = "/slop/factory/activity",
    method = "GET",
    accept = "text/html",
    status = 200,
    headers = publicHeaders,
    encoding = "identity",
  } = {}) {
    const type = accept === "text/html" ? "text/html" : "application/json";
    const body =
      type === "text/html" ? "<html>Activity</html>" : '{"daily":[]}';
    return handle({
      event: {
        route: { id },
        request: new Request(`https://jomcgi.dev${path}`, {
          method,
          headers: {
            accept,
            "accept-encoding": encoding,
            "if-none-match": '"old-activity"',
          },
        }),
      },
      resolve: async () =>
        new Response(method === "HEAD" ? null : body, {
          status,
          headers: { ...headers, "content-type": type },
        }),
    });
  }

  it.each(["text/html", "application/json", "*/*"])(
    "does not store or revalidate a negotiated %s response",
    async (accept) => {
      const response = await respond({ accept });
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(response.headers.get("cloudflare-cdn-cache-control")).toBe(
        "no-store",
      );
      expect(response.headers.has("etag")).toBe(false);
      expect(response.headers.get("vary")).toBe("Accept");
      expect(response.headers.get("content-type")).toBe(
        accept === "text/html" ? "text/html" : "application/json",
      );
      expect(await response.text()).toBe(
        accept === "text/html" ? "<html>Activity</html>" : '{"daily":[]}',
      );
    },
  );

  it.each(["text/html", "application/json", "*/*"])(
    "applies the same policy before the HEAD fast path for %s",
    async (accept) => {
      const response = await respond({ method: "HEAD", accept });
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(response.headers.get("cloudflare-cdn-cache-control")).toBe(
        "no-store",
      );
      expect(response.headers.has("etag")).toBe(false);
      expect(await response.text()).toBe("");
    },
  );

  it.each([200, 500, 503])(
    "also protects fallback/error status %s",
    async (status) => {
      const response = await respond({ status, headers: {} });
      expect(response.status).toBe(status);
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(response.headers.get("cloudflare-cdn-cache-control")).toBe(
        "no-store",
      );
    },
  );

  it("protects navigation data and filtered URLs by resolved route", async () => {
    for (const path of [
      "/slop/factory/activity/__data.json?x-sveltekit-invalidated=001",
      "/slop/factory/activity?state=queued",
      "/public/slop/factory/activity",
    ]) {
      const response = await respond({
        path,
        accept: "application/json",
        encoding: "gzip",
      });
      expect(response.headers.get("cache-control")).toBe("no-store");
      expect(response.headers.get("cloudflare-cdn-cache-control")).toBe(
        "no-store",
      );
      expect(response.headers.has("etag")).toBe(false);
      expect(await response.json()).toEqual({ daily: [] });
    }
  });

  it.each([
    "/public/slop/factory/data/activity",
    "/public/slop/factory/data/board",
    "/public/slop/factory/activity/[issue]",
    "/private/factory",
    null,
  ])("preserves the cache contract of %s", async (id) => {
    const response = await respond({ id });
    for (const [key, value] of Object.entries(publicHeaders)) {
      expect(response.headers.get(key)).toBe(value);
    }
  });

  it.each([
    ["application/json", "text/html"],
    ["text/html", "application/json"],
  ])(
    "cannot seed a URL-only cache with %s before %s",
    async (first, second) => {
      const cache = new Map();
      for (const accept of [first, second, first, second]) {
        const key = "https://jomcgi.dev/slop/factory/activity";
        const response = cache.get(key)?.clone() ?? (await respond({ accept }));
        if (
          !response.headers
            .get("cloudflare-cdn-cache-control")
            ?.includes("no-store")
        ) {
          cache.set(key, response.clone());
        }
        expect(response.headers.get("content-type")).toBe(
          accept === "text/html" ? "text/html" : "application/json",
        );
      }
      expect(cache.size).toBe(0);
    },
  );
});
