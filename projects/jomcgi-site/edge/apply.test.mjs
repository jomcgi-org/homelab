import assert from "node:assert/strict";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import http from "node:http";
import test from "node:test";
import { applyCacheSettings, cloudflareClient } from "./apply.mjs";

const file = JSON.parse(
  await readFile(new URL("./cache-settings.json", import.meta.url), "utf8"),
);
const accountId = "a".repeat(32);
const zoneId = "b".repeat(32);
const phase = "http_request_cache_settings";
const endpoint = `/client/v4/zones/${zoneId}/rulesets/phases/${phase}/entrypoint`;

const monolith = {
  description: "Monolith",
  expression:
    '(http.host eq "jomcgi.dev") or (http.host eq "private.jomcgi.dev")',
  action: "set_cache_settings",
  action_parameters: {
    cache: true,
    edge_ttl: { mode: "bypass_by_default" },
    respect_strong_etags: true,
    serve_stale: { disable_stale_while_updating: false },
  },
  enabled: true,
};

// Exercise the HTTP client as well as the apply logic. Production requests
// still address api.cloudflare.com over IPv4; only this test transport is local.
async function api(t, { rules = [monolith], afterPut, reply, zones } = {}) {
  let live = structuredClone(rules);
  const calls = [];
  const server = http.createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    calls.push({ method: req.method, path: req.url, body });
    assert.equal(req.headers.authorization, "Bearer test-token");
    assert.equal(req.headers["content-type"], "application/json");
    if (reply?.(req, res)) return;
    let result;
    if (req.url.startsWith("/client/v4/zones?")) {
      const query = new URL(req.url, "http://localhost").searchParams;
      assert.equal(query.get("name"), "jomcgi.dev");
      assert.equal(query.get("account.id"), accountId);
      assert.equal(req.method, "GET");
      result = zones ?? [
        { name: "jomcgi.dev", id: zoneId, account: { id: accountId } },
      ];
    } else {
      assert.equal(req.url, endpoint);
      if (req.method === "PUT") {
        live = JSON.parse(body).rules;
        if (afterPut) live = afterPut(live);
      }
      result = {
        kind: "zone",
        phase,
        rules: live.map((rule, i) => ({
          ...rule,
          id: `rule-${i}`,
          ref: `rule-${i}`,
          version: String(calls.length),
          last_updated: "2026-11-17T00:00:00Z",
        })),
      };
    }
    res.setHeader("Content-Type", "application/json");
    res.end(JSON.stringify({ success: true, result }));
  });
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => new Promise((resolve) => server.close(resolve)));
  const client = cloudflareClient("test-token", (url, options, callback) => {
    assert.equal(url.origin, "https://api.cloudflare.com");
    assert.equal(options.family, 4);
    assert.ok(options.signal instanceof AbortSignal);
    return http.request(
      new URL(
        url.pathname + url.search,
        `http://127.0.0.1:${server.address().port}`,
      ),
      options,
      callback,
    );
  });
  return {
    calls,
    apply: (desired = file) =>
      applyCacheSettings({ file: desired, accountId, client }),
  };
}

test("imports Monolith unchanged and respects object TTLs on data.jomcgi.dev", () => {
  assert.deepEqual(file.rules, [
    monolith,
    {
      description: "data.jomcgi.dev",
      expression: 'http.host eq "data.jomcgi.dev"',
      action: "set_cache_settings",
      action_parameters: {
        cache: true,
        edge_ttl: { mode: "respect_origin" },
        browser_ttl: { mode: "respect_origin" },
        serve_stale: { disable_stale_while_updating: false },
      },
      enabled: true,
    },
  ]);
});

test("a second apply sends the same complete PUT body and verifies each write", async (t) => {
  const { apply, calls } = await api(t);
  await apply();
  await apply();
  assert.deepEqual(
    calls.map(({ method }) => method),
    ["GET", "GET", "PUT", "GET", "GET", "GET", "PUT", "GET"],
  );
  const puts = calls.filter(({ method }) => method === "PUT");
  assert.equal(puts[0].path, endpoint);
  assert.equal(puts[0].body, JSON.stringify(file));
  assert.equal(puts[1].body, puts[0].body);
});

for (const rule of [
  { ...monolith, description: "Dashboard only" },
  { ...monolith, expression: '(http.host eq "other.jomcgi.dev")' },
  { ...monolith, description: "Disabled dashboard rule", enabled: false },
]) {
  test(`stops before PUT and names an unlisted live rule: ${rule.description} ${rule.expression}`, async (t) => {
    const { apply, calls } = await api(t, { rules: [monolith, rule] });
    await assert.rejects(apply(), (error) => {
      assert.ok(error.message.includes(JSON.stringify(rule.description)));
      assert.ok(error.message.includes(rule.expression));
      return true;
    });
    assert.deepEqual(
      calls.map(({ method }) => method),
      ["GET", "GET"],
    );
  });
}

test("updates settings for a rule with the same description and expression", async (t) => {
  const { apply } = await api(t, { rules: [{ ...monolith, enabled: false }] });
  await apply();
});

for (const [name, afterPut] of [
  ["settings", (rules) => [{ ...rules[0], enabled: false }, rules[1]]],
  ["order", (rules) => rules.toReversed()],
  ["missing rule", (rules) => rules.slice(0, 1)],
  [
    "extra setting",
    (rules) => [
      {
        ...rules[0],
        action_parameters: {
          ...rules[0].action_parameters,
          origin_error_page_passthru: true,
        },
      },
      rules[1],
    ],
  ],
]) {
  test(`fails readback verification on changed ${name}`, async (t) => {
    const { apply, calls } = await api(t, { afterPut });
    await assert.rejects(apply(), /verification failed/);
    assert.deepEqual(
      calls.map(({ method }) => method),
      ["GET", "GET", "PUT", "GET"],
    );
  });
}

for (const [status, response, error] of [
  [403, { success: false }, /HTTP 403/],
  [404, { success: false }, /HTTP 404/],
  [429, { success: false }, /HTTP 429/],
  [302, { success: false }, /HTTP 302/],
  [200, { success: false }, /unsuccessful API response/],
  [200, { success: true, result: {} }, /Invalid cache-settings entrypoint/],
  [
    200,
    { success: true, result: { phase, kind: "zone", rules: null } },
    /Invalid cache-settings entrypoint/,
  ],
  [200, "not JSON", /invalid JSON response/],
]) {
  test(`fails closed before PUT on read error: ${status} ${JSON.stringify(response)}`, async (t) => {
    const { apply, calls } = await api(t, {
      reply(req, res) {
        if (req.url !== endpoint) return false;
        res.writeHead(status);
        res.end(
          typeof response === "string" ? response : JSON.stringify(response),
        );
        return true;
      },
    });
    await assert.rejects(apply(), error);
    assert.ok(calls.every(({ method }) => method === "GET"));
  });
}

test("a failed PUT fails the apply", async (t) => {
  const { apply, calls } = await api(t, {
    reply(req, res) {
      if (req.method !== "PUT") return false;
      res.writeHead(500);
      res.end("unavailable");
      return true;
    },
  });
  await assert.rejects(apply(), /HTTP 500/);
  assert.deepEqual(
    calls.map(({ method }) => method),
    ["GET", "GET", "PUT"],
  );
});

for (const zones of [
  [],
  [{ name: "other.dev", id: zoneId, account: { id: accountId } }],
]) {
  test(`rejects a missing or mismatched zone: ${JSON.stringify(zones)}`, async (t) => {
    const { apply, calls } = await api(t, { zones });
    await assert.rejects(apply(), /Expected exactly one jomcgi.dev zone/);
    assert.equal(calls.length, 1);
  });
}

for (const invalid of [
  {},
  { rules: [monolith, monolith] },
  { rules: [{ ...monolith, id: "live-id" }] },
]) {
  test(`rejects an invalid file before contacting Cloudflare: ${JSON.stringify(invalid)}`, async (t) => {
    const { apply, calls } = await api(t);
    await assert.rejects(apply(invalid));
    assert.equal(calls.length, 0);
  });
}
