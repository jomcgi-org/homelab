import assert from "node:assert/strict";
import { once } from "node:events";
import { createServer } from "node:http";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { createLandingServer, STATUS_TIMEOUT_MS } from "./server.mjs";

async function listen(t, server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => {
    server.closeAllConnections();
    server.close();
  });
  return `http://127.0.0.1:${server.address().port}`;
}

test("proxies the fixed status URL without forwarding credentials or query parameters", async (t) => {
  const snapshot = {
    needsYou: [],
    inFlight: [],
    shipped: [],
    generatedAt: "2026-11-14T12:00:00Z",
  };
  const upstream = await listen(
    t,
    createServer((req, res) => {
      assert.equal(req.url, "/status");
      assert.equal(req.method, "GET");
      assert.equal(req.headers.cookie, undefined);
      assert.equal(req.headers.authorization, undefined);
      res.writeHead(200, {
        "Content-Type": "application/json",
        "Set-Cookie": "private=secret",
      });
      res.end(JSON.stringify(snapshot));
    }),
  );
  const url = await listen(
    t,
    createLandingServer({ statusURL: `${upstream}/status` }),
  );
  const response = await fetch(
    `${url}/api/status?url=https://untrusted.invalid`,
    {
      headers: { Cookie: "session=private", Authorization: "Bearer private" },
    },
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), snapshot);
  assert.equal(response.headers.get("set-cookie"), null);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.match(
    response.headers.get("content-security-policy"),
    /connect-src 'self';/,
  );
});

test("times out headers and slow bodies after two seconds", async (t) => {
  assert.equal(STATUS_TIMEOUT_MS, 2000);
  for (const writeHeaders of [false, true]) {
    await t.test(writeHeaders ? "body" : "headers", async (t) => {
      const upstream = await listen(
        t,
        createServer((_req, res) => {
          if (writeHeaders) {
            res.writeHead(200);
            res.write('{"needsYou":');
          }
        }),
      );
      const url = await listen(
        t,
        createLandingServer({ statusURL: `${upstream}/status` }),
      );
      const started = Date.now();
      const response = await fetch(`${url}/api/status`);
      assert.equal(response.status, 503);
      assert.ok(Date.now() - started >= 1900);
      assert.ok(Date.now() - started < 4000);
    });
  }
});

test("upstream errors, malformed JSON, redirects and oversized bodies return a generic 503", async (t) => {
  for (const reply of [
    (res) => res.writeHead(500).end("private error"),
    (res) => res.end("not JSON"),
    (res) => res.writeHead(302, { Location: "/private" }).end(),
    (res) => res.end('"' + "x".repeat(1024 * 1024) + '"'),
  ]) {
    const upstream = await listen(
      t,
      createServer((_req, res) => reply(res)),
    );
    const url = await listen(
      t,
      createLandingServer({ statusURL: `${upstream}/status` }),
    );
    const response = await fetch(`${url}/api/status`);
    assert.equal(response.status, 503);
    assert.deepEqual(await response.json(), { error: "Status unavailable" });
  }
});

test("a missing endpoint leaves static files available and API methods read-only", async (t) => {
  const root = await mkdtemp(join(tmpdir(), "factory-landing-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await writeFile(join(root, "index.html"), "<h1>Factory</h1>");
  const url = await listen(t, createLandingServer({ root, statusURL: "" }));
  assert.equal((await fetch(`${url}/api/status`)).status, 503);
  for (const method of ["POST", "HEAD", "DELETE"]) {
    assert.equal((await fetch(`${url}/api/status`, { method })).status, 405);
  }
  assert.equal(await (await fetch(url)).text(), "<h1>Factory</h1>");
  assert.equal((await fetch(`${url}/api/other`)).status, 404);
});

test("Needs you pages retain order and clamp after removal", async () => {
  const { pageOf } = await import("./web/src/paging.mjs");
  const items = [1, 2, 3, 4, 5, 6];
  assert.deepEqual(pageOf(items, 0).items, items.slice(0, 5));
  assert.deepEqual(pageOf(items, 1).items, [6]);
  assert.equal(pageOf(items.slice(0, 5), 1).page, 0);
});

test("only Joe's same-origin decision reaches the connector with the displayed revision", async (t) => {
  const revision = '"' + "a".repeat(64) + '"';
  let calls = 0;
  const url = await listen(
    t,
    createLandingServer({
      joeSubject: "subject",
      statusURL: "http://connector/status",
      readToken: async () => "projected",
      fetchStatus: async (target, options) => {
        calls++;
        assert.equal(target.href, "http://connector/decide");
        assert.equal(options.method, "POST");
        assert.equal(options.headers.Authorization, "Bearer projected");
        assert.equal(options.headers["If-Match"], revision);
        assert.deepEqual(JSON.parse(options.body), {
          key: "HOME-42",
          choice: 2,
        });
        return new Response(null, { status: 204 });
      },
    }),
  );
  for (const [subject, origin, status] of [
    ["other", "https://factory.jomcgi.dev", 403],
    ["subject", "https://example.org", 403],
    ["subject", "https://factory.jomcgi.dev", 204],
  ]) {
    const result = await fetch(url + "/api/decide", {
      method: "POST",
      headers: {
        "x-runbook-user": subject,
        origin,
        "if-match": revision,
        "content-type": "application/json",
      },
      body: JSON.stringify({ key: "HOME-42", choice: 2 }),
    });
    assert.equal(result.status, status);
  }
  assert.equal(calls, 1);
});
