import { createServer, request } from "node:http";
import { once } from "node:events";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { protectResponseCache } from "../server/http-cache-policy.js";

let server;
const fixtures = new Map();
let fixtureId = 0;

beforeAll(async () => {
  server = createServer((req, res) => {
    protectResponseCache(req, res);
    const fixture = fixtures.get(req.headers["x-fixture"]);
    for (const [name, value] of Object.entries(fixture.initial || {}))
      res.setHeader(name, value);
    if (fixture.reason) res.writeHead(200, fixture.reason, fixture.headers);
    else res.writeHead(200, fixture.headers);
    res.end("unchanged body");
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
});

afterAll(async () => {
  await new Promise((resolve) => server.close(resolve));
});

function read(fixture, headers = {}, path = "/app/hikes") {
  const id = String(++fixtureId);
  fixtures.set(id, fixture);
  return new Promise((resolve, reject) => {
    const req = request(
      {
        hostname: "127.0.0.1",
        port: server.address().port,
        path,
        headers: { host: "jomcgi.dev", "x-fixture": id, ...headers },
      },
      (res) => {
        let body = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => (body += chunk));
        res.on("error", reject);
        res.on("end", () =>
          resolve({ status: res.statusCode, headers: res.headers, body }),
        );
      },
    );
    req.on("error", reject);
    req.end();
  });
}

const publicHeaders = {
  "Cache-Control": "public, max-age=300",
  "Cloudflare-CDN-Cache-Control": "public, max-age=300",
  "CDN-Cache-Control": "public, max-age=300",
  ETag: '"shared"',
  "X-Preserved": "yes",
};

describe("final HTTP cache headers", () => {
  it.each([
    { headers: publicHeaders },
    { headers: Object.entries(publicHeaders).flat() },
    { reason: "OK", headers: publicHeaders },
    { reason: "OK", headers: Object.entries(publicHeaders).flat() },
  ])(
    "cannot override cookie isolation through writeHead: %j",
    async (fixture) => {
      const result = await read(fixture, { cookie: "gcs=test-session" });
      expect(result.headers["cache-control"]).toBe("no-store");
      expect(result.headers["cloudflare-cdn-cache-control"]).toBe("no-store");
      expect(result.headers["cdn-cache-control"]).toBe("no-store");
      expect(result.headers.etag).toBeUndefined();
      expect(result.headers["x-preserved"]).toBe("yes");
      expect(result.body).toBe("unchanged body");
      expect(result.status).toBe(200);
    },
  );

  it.each([
    { "cache-control": "private" },
    { "cloudflare-cdn-cache-control": "no-cache" },
    { "cdn-cache-control": "no-store" },
    { vary: "Cookie" },
    { "set-cookie": "gcs=test-session" },
  ])(
    "retains restrictive setHeader intent when writeHead conflicts: %j",
    async (initial) => {
      const result = await read({ initial, headers: publicHeaders });
      expect(result.headers["cache-control"]).toBe("no-store");
      expect(result.headers["cloudflare-cdn-cache-control"]).toBe("no-store");
      expect(result.headers.etag).toBeUndefined();
    },
  );

  it.each([
    "/private/factory/",
    "/friends/trips/",
    "/public/app/grimoire/chat/",
  ])("protects explicit restricted paths: %s", async (path) => {
    const result = await read({ headers: publicHeaders }, {}, path);
    expect(result.headers["cache-control"]).toBe("no-store");
  });

  it("uses the incoming private host for absolute request targets", async () => {
    const result = await read(
      { headers: publicHeaders },
      { host: "private.jomcgi.dev" },
      "http://jomcgi.dev/app/hikes/",
    );
    expect(result.headers["cache-control"]).toBe("no-store");
  });

  it("preserves explicit anonymous policies and validators", async () => {
    const result = await read({ headers: publicHeaders });
    expect(result.headers["cache-control"]).toBe("public, max-age=300");
    expect(result.headers["cloudflare-cdn-cache-control"]).toBe(
      "public, max-age=300",
    );
    expect(result.headers.etag).toBe('"shared"');
    expect(result.body).toBe("unchanged body");
  });
});
