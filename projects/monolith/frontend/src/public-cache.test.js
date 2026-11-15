import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { gunzipSync } from "node:zlib";
import { handle } from "./hooks.server.js";
import { reroute } from "./hooks.js";
import {
  cloudflareCacheHeaders,
  HIKES_WALKS_CACHE_CONTROL,
  SHIPS_SNAPSHOT_CACHE_CONTROL,
  SHIPS_HEAT_CACHE_CONTROL,
  SHIPS_TRACK_CACHE_CONTROL,
  STARS_SITES_CACHE_CONTROL,
  STARS_HISTORY_CACHE_CONTROL,
  TRIPS_CACHE_CONTROL,
  PAGE_CACHE_CONTROL,
  GRIMOIRE_READ_CACHE_CONTROL,
  STATS_CACHE_CONTROL,
} from "./lib/cache-headers.js";
import { load as hikes } from "./routes/public/app/hikes/+page.server.js";
import { load as ships } from "./routes/public/app/ships/+page.server.js";
import { load as stars } from "./routes/public/app/stars/+page.server.js";
import { load as trips } from "./routes/public/app/trips/+page.server.js";
import { load as trip } from "./routes/public/app/trips/[slug]/+layout.server.js";
import { load as leaderboard } from "./routes/public/app/llm-leaderboard/+page.server.js";
import { GET as walk } from "./routes/public/app/hikes/walk/[uuid]/+server.js";
import { GET as heat } from "./routes/public/app/ships/heat/+server.js";
import { GET as track } from "./routes/public/app/ships/track/[mmsi]/+server.js";
import { GET as starMap } from "./routes/public/app/stars/map/+server.js";
import { GET as starHistory } from "./routes/public/app/stars/history/+server.js";
import { GET as starHistoryMap } from "./routes/public/app/stars/history-map/+server.js";
import { GET as grimoireRead } from "./routes/public/app/grimoire/api/[...path]/+server.js";
import { GET as bookRead } from "./routes/public/app/grimoire/book/[book]/read/+server.js";
import { GET as noteBody } from "./routes/public/app/notes/body/[id]/+server.js";
import { GET as noteStats } from "./routes/public/app/notes/stats/+server.js";

// Discover the shipped route set so a newly added page or proxy also exercises
// the origin boundary. +page@.svelte is the shared-chat layout reset.
const appRoutes = [
  ...new Set(
    Object.keys(
      import.meta.glob([
        "./routes/public/app/**/+page*.svelte",
        "./routes/public/app/**/+page.server.js",
        "./routes/public/app/**/+server.js",
        "./routes/public/scotland/**/+page*.svelte",
        "./routes/public/scotland/**/+server.js",
      ]),
    ).map((path) => path.replace(/^\.\/routes/, "").replace(/\/\+[^/]+$/, "")),
  ),
];
const publicHeaders = {
  ...cloudflareCacheHeaders(HIKES_WALKS_CACHE_CONTROL),
  etag: '"previous-public-response"',
  "content-type": "text/html",
};

function respond({
  id = "/public/app/hikes",
  url = "https://jomcgi.dev/app/hikes",
  method = "GET",
  requestHeaders = {},
  headers = publicHeaders,
  status = 200,
  body = "public data",
} = {}) {
  return handle({
    event: {
      route: { id },
      request: new Request(url, { method, headers: requestHeaders }),
    },
    resolve: async () =>
      new Response(method === "HEAD" || status === 304 ? null : body, {
        status,
        headers,
      }),
  });
}

function expectNoStore(response) {
  expect(response.headers.get("cache-control")).toBe("no-store");
  expect(response.headers.get("cloudflare-cdn-cache-control")).toBe("no-store");
  expect(response.headers.has("etag")).toBe(false);
}

describe("public route cache isolation", () => {
  it("covers the main apps, proxies, shared chats and Scotland", () => {
    expect(appRoutes).toEqual(
      expect.arrayContaining([
        "/public/app/hikes",
        "/public/app/ships/track/[mmsi]",
        "/public/app/stars/history-map",
        "/public/app/trips/[slug]/day/[day]",
        "/public/app/notes/body/[id]",
        "/public/app/grimoire/chat/s/[id]",
        "/public/app/frank",
        "/public/app/firecracker",
        "/public/scotland",
      ]),
    );
  });

  it.each(appRoutes)(
    "never stores session or authenticated responses from %s",
    async (id) => {
      for (const requestHeaders of [
        { cookie: "gcs=test-session" },
        { authorization: "Bearer test-session" },
        { "cf-access-jwt-assertion": "test-session" },
        { "x-auth-email": "test@example.com" },
      ]) {
        const response = await respond({ id, requestHeaders });
        expectNoStore(response);
        expect(await response.text()).toBe("public data");
      }
    },
  );

  it.each(appRoutes)(
    "requires an explicit anonymous cache policy for %s",
    async (id) => {
      expectNoStore(
        await respond({ id, headers: { "content-type": "text/html" } }),
      );
    },
  );

  it.each(["GET", "HEAD", "POST"])(
    "protects %s before compression and body fast paths",
    async (method) => {
      for (const type of [
        "text/html",
        "application/json",
        "text/event-stream",
      ]) {
        const response = await respond({
          method,
          headers: { ...publicHeaders, "content-type": type },
          requestHeaders: {
            cookie: "gcs=test-session",
            "accept-encoding": "gzip",
          },
        });
        expectNoStore(response);
        const bytes = Buffer.from(await response.arrayBuffer());
        const body =
          response.headers.get("content-encoding") === "gzip"
            ? gunzipSync(bytes).toString()
            : bytes.toString();
        expect(body).toBe(method === "HEAD" ? "" : "public data");
      }
    },
  );

  it.each(["POST", "PUT", "PATCH", "DELETE"])(
    "never stores %s responses",
    async (method) => {
      expectNoStore(await respond({ method }));
    },
  );

  it.each([
    "/private/factory",
    "/friends/moving",
    "/friends/grimoire",
    "/private",
    "/friends",
  ])("protects the authenticated route %s regardless of host", async (id) =>
    expectNoStore(await respond({ id })),
  );

  it.each(["private.jomcgi.dev", "friends.jomcgi.dev"])(
    "protects public route aliases and errors on %s",
    async (host) => {
      for (const path of [
        "/app/hikes",
        "/public/app/hikes",
        "/does-not-exist",
      ]) {
        const url = new URL(`https://${host}${path}`);
        const id = path === "/does-not-exist" ? null : reroute({ url });
        expectNoStore(await respond({ url: url.href, id }));
        // A crafted/reused public route ID cannot override the authenticated host.
        expectNoStore(
          await respond({ url: url.href, id: "/public/app/hikes" }),
        );
      }
    },
  );

  it.each([400, 401, 403, 404, 429, 500, 503])(
    "does not cache status %s with inherited public headers",
    async (status) => {
      expectNoStore(await respond({ status }));
    },
  );

  it("does not cache an unmatched route or a cookie-dependent chat shell", async () => {
    expectNoStore(await respond({ id: null }));
    expectNoStore(await respond({ id: "/public/app/grimoire/chat" }));
  });

  it("protects cookies emitted by the resolved handler", async () => {
    const response = await respond({
      headers: { ...publicHeaders, "set-cookie": "gcs=test; HttpOnly" },
    });
    expectNoStore(response);
    expect(response.headers.get("set-cookie")).toBe("gcs=test; HttpOnly");
  });

  it.each([
    "cache-control",
    "cdn-cache-control",
    "cloudflare-cdn-cache-control",
  ])(
    "honours restrictive %s even when the other policy says public",
    async (header) => {
      for (const policy of [
        "private, max-age=600",
        'private="set-cookie"',
        "no-store",
        'no-cache="authorization"',
      ]) {
        const response = await respond({
          headers: { ...publicHeaders, [header]: policy },
        });
        expectNoStore(response);
        if (header === "cdn-cache-control")
          expect(response.headers.get(header)).toBe("no-store");
      }
    },
  );

  it.each(["Cookie", "Authorization", "Accept-Encoding, Cookie", "*"])(
    "does not share responses with Vary: %s",
    async (vary) => {
      expectNoStore(await respond({ headers: { ...publicHeaders, vary } }));
    },
  );

  it("leaves unrelated anonymous routes without a policy unchanged", async () => {
    const response = await respond({ id: "/public/blog/[slug]", headers: {} });
    expect(response.headers.has("cache-control")).toBe(false);
    expect(response.headers.has("cloudflare-cdn-cache-control")).toBe(false);
  });
});

describe("real public app loader cache policies", () => {
  it.each([
    ["/public/app/hikes", hikes, HIKES_WALKS_CACHE_CONTROL],
    ["/public/app/ships", ships, SHIPS_SNAPSHOT_CACHE_CONTROL],
    ["/public/app/stars", stars, STARS_SITES_CACHE_CONTROL],
    ["/public/app/trips", trips, TRIPS_CACHE_CONTROL],
    ["/public/app/trips/[slug]", trip, TRIPS_CACHE_CONTROL],
    ["/public/app/trips/[slug]/day/[day]", trip, TRIPS_CACHE_CONTROL],
    ["/public/app/trips/[slug]/timeline", trip, TRIPS_CACHE_CONTROL],
    ["/public/app/llm-leaderboard", leaderboard, PAGE_CACHE_CONTROL],
  ])(
    "preserves %s for anonymous visitors and isolates cookie requests",
    async (id, load, policy) => {
      for (const requestHeaders of [{}, { cookie: "gcs=test-session" }]) {
        const response = await handle({
          event: {
            route: { id },
            request: new Request("https://jomcgi.dev/app/test", {
              headers: requestHeaders,
            }),
          },
          resolve: async () => {
            const headers = new Headers({ "content-type": "text/html" });
            await load({
              params: { slug: "test-trip" },
              fetch: vi.fn(
                async () =>
                  new Response(
                    JSON.stringify({ trips: [], trip: null, points: [] }),
                    {
                      headers: { etag: '"fixture-data"' },
                    },
                  ),
              ),
              setHeaders: (values) => {
                for (const [name, value] of Object.entries(values))
                  headers.set(name, value);
              },
            });
            return new Response("fixture first view", { headers });
          },
        });
        if (requestHeaders.cookie) expectNoStore(response);
        else
          for (const [name, value] of Object.entries(
            cloudflareCacheHeaders(policy),
          )) {
            expect(response.headers.get(name)).toBe(value);
          }
        expect(await response.text()).toBe("fixture first view");
      }
    },
  );
});

describe("real public app proxy cache policies", () => {
  beforeEach(() => vi.stubEnv("API_BASE", "http://test-backend"));
  afterEach(() => vi.unstubAllEnvs());

  it.each([
    ["/public/app/hikes/walk/[uuid]", walk, HIKES_WALKS_CACHE_CONTROL],
    ["/public/app/ships/heat", heat, SHIPS_HEAT_CACHE_CONTROL],
    ["/public/app/ships/track/[mmsi]", track, SHIPS_TRACK_CACHE_CONTROL],
    ["/public/app/stars/map", starMap, STARS_SITES_CACHE_CONTROL],
    ["/public/app/stars/history", starHistory, STARS_HISTORY_CACHE_CONTROL],
    [
      "/public/app/stars/history-map",
      starHistoryMap,
      STARS_HISTORY_CACHE_CONTROL,
    ],
    [
      "/public/app/grimoire/api/[...path]",
      grimoireRead,
      GRIMOIRE_READ_CACHE_CONTROL,
    ],
    [
      "/public/app/grimoire/book/[book]/read",
      bookRead,
      GRIMOIRE_READ_CACHE_CONTROL,
    ],
    ["/public/app/notes/stats", noteStats, STATS_CACHE_CONTROL],
    [
      "/public/app/notes/body/[id]",
      noteBody,
      "public, max-age=60, s-maxage=60, must-revalidate",
    ],
  ])(
    "preserves anonymous %s and overrides cookie responses",
    async (id, GET, policy) => {
      for (const requestHeaders of [{}, { cookie: "gcs=test-session" }]) {
        const response = await handle({
          event: {
            route: { id },
            request: new Request("https://jomcgi.dev/app/test", {
              headers: requestHeaders,
            }),
          },
          resolve: async () => {
            const headers = new Headers();
            const response = await GET({
              params: {
                id: "test",
                book: "test",
                path: "books",
                uuid: "test",
                mmsi: "123456789",
              },
              url: new URL("https://jomcgi.dev/app/test"),
              fetch: vi.fn(
                async () =>
                  new Response(
                    JSON.stringify({ items: [], next_cursor: null }),
                    {
                      headers: {
                        "cache-control":
                          "public, max-age=60, s-maxage=60, must-revalidate",
                      },
                    },
                  ),
              ),
              setHeaders: (values) => {
                for (const [name, value] of Object.entries(values))
                  headers.set(name, value);
              },
            });
            for (const [name, value] of headers)
              response.headers.set(name, value);
            return response;
          },
        });
        if (requestHeaders.cookie) expectNoStore(response);
        else
          for (const [name, value] of Object.entries(
            cloudflareCacheHeaders(policy),
          )) {
            expect(response.headers.get(name)).toBe(value);
          }
        expect((await response.json()).items).toEqual([]);
      }
    },
  );
});
