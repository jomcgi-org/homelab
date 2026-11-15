import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// Run the packaged entry: direct hook tests cannot see adapter-level responses.
const observations = JSON.parse(
  execFileSync(
    process.execPath,
    [
      "--input-type=module",
      "-e",
      `
    import { request } from "node:http";
    import { once } from "node:events";
    import { readdirSync } from "node:fs";
    process.env.PORT = "0";
    process.env.HOST = "127.0.0.1";
    process.env.PUBLIC_SNAPSHOT_PREWARM = "false";
    process.env.PRIVATE_DASHBOARD_PREWARM = "false";
    const fetches = [];
    globalThis.fetch = async (input) => {
      fetches.push(String(input));
      throw new Error("Cache boundary cases must not fetch backend data");
    };
    const log = console.log;
    console.log = () => {};
    const { server } = await import("./dist/cache-server.js");
    const http = server.server;
    if (!http.listening) await once(http, "listening");
    const cases = [];
    for (const path of ["/app/hikes/", "/app/ships/", "/app/stars/", "/app/trips/", "/app/notes/", "/app/grimoire/chat/"]) {
      for (const method of ["GET", "HEAD"]) {
        cases.push({ path, method, headers: { cookie: "gcs=test-session" }, redirect: path.slice(0, -1) });
      }
    }
    cases.push(
      { path: "/app/hikes/", headers: { authorization: "Bearer test-session" }, redirect: "/app/hikes" },
      { path: "/app/hikes/", headers: { "cf-access-jwt-assertion": "test-session" }, redirect: "/app/hikes" },
      { path: "/app/hikes/", headers: { "x-auth-email": "test@example.com" }, redirect: "/app/hikes" },
      { path: "/app/hikes", method: "POST", headers: { "content-type": "application/x-www-form-urlencoded", origin: "https://different.example" }, error: 403 },
      { path: "/not-a-route/", headers: { cookie: "gcs=test-session" }, error: 404 },
      { path: "/not-a-route/", headers: { host: "private.jomcgi.dev" }, error: 404 },
      { path: "/not-a-route/", headers: { host: "friends.jomcgi.dev" }, error: 404 },
      { path: "/app/frank", anonymous: true },
      { path: "/app/frank", headers: { host: "monolith-public-frontend.monolith-public.svc.cluster.local:3000" }, anonymous: true },
      { path: "/app/frank", headers: { host: "monolith.monolith.svc.cluster.local:3000" }, error: 404 },
      { path: "/app/frank", headers: { host: "friends.jomcgi.dev" }, error: 404 },
      { path: "/app/frank", headers: { host: "unrelated.svc.cluster.local:3000" }, error: 404 },
      { path: "/private/factory", headers: { host: "monolith-public-frontend.monolith-public.svc.cluster.local:3000" }, error: 404 },
      { path: "/friends/moving", headers: { host: "monolith-public-frontend.monolith-public.svc.cluster.local:3000" }, error: 404 }
    );
    for (const identity of ["cookie", "authorization", "cf-access-jwt-assertion", "x-auth-email"]) {
      cases.push({ path: "/app/frank", headers: { host: "monolith-public-frontend.monolith-public.svc.cluster.local:3000", [identity]: "test-session" } });
    }
    const asset = readdirSync("./dist/client/_app/immutable/entry").find((name) => name.startsWith("start.") && name.endsWith(".js"));
    cases.push({ path: "/_app/immutable/entry/" + asset, headers: { cookie: "gcs=test-session" }, asset: true });
    const results = [];
    try {
      for (const entry of cases) {
        results.push(await new Promise((resolve, reject) => {
          const req = request({
            hostname: "127.0.0.1", port: http.address().port, path: entry.path,
            method: entry.method || "GET",
            headers: { host: "jomcgi.dev", accept: "text/html", ...entry.headers },
          }, (response) => {
            let body = "";
            response.setEncoding("utf8");
            response.on("data", (chunk) => body += chunk);
            response.on("error", reject);
            response.on("end", () => resolve({ ...entry, status: response.statusCode, headers: response.headers, body }));
          });
          req.on("error", reject);
          req.end();
        }));
      }
    } finally {
      await new Promise((resolve) => http.close(resolve));
    }
    console.log = log;
    console.log(JSON.stringify({ results, fetches }));
  `,
    ],
    {
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      encoding: "utf8",
      timeout: 30_000,
    },
  ),
);

describe("packaged HTTP cache boundary", () => {
  it.each(observations.results.filter((result) => !result.anonymous))(
    "protects $method $path before any handler can write headers",
    (result) => {
      expect(result.headers["cache-control"]).toBe("no-store");
      expect(result.headers["cloudflare-cdn-cache-control"]).toBe("no-store");
      expect(result.headers.etag).toBeUndefined();
      if (result.redirect) {
        expect(result.status).toBe(308);
        expect(result.headers.location).toBe(result.redirect);
      } else if (result.error) expect(result.status).toBe(result.error);
      else expect(result.status).toBe(200);
      if (result.method === "HEAD") expect(result.body).toBe("");
      if (result.asset) expect(result.body.length).toBeGreaterThan(0);
    },
  );

  it.each(observations.results.filter((entry) => entry.anonymous))(
    "preserves the anonymous static page on the public and screenshot origins: $headers.host",
    (result) => {
      expect(result.status).toBe(200);
      expect(result.headers["cache-control"]).toBe("public, max-age=300");
      expect(result.body.slice(0, 15)).toBe("<!doctype html>");
      const apex = observations.results.find((entry) => entry.anonymous);
      expect(result.body).toBe(apex.body);
    },
  );

  it("does not execute backend loads for normalization redirects", () => {
    expect(observations.fetches).toEqual([]);
  });
});
