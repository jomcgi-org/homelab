import assert from "node:assert/strict";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import http from "node:http";
import test from "node:test";
import { applyCacheSettings, cloudflareClient } from "./apply.mjs";

const file = JSON.parse(
  await readFile(new URL("./cache-settings.json", import.meta.url), "utf8"),
);
const imported = JSON.parse(
  await readFile(
    new URL("./imported/2026-11-17.json", import.meta.url),
    "utf8",
  ),
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
          ref: rule.ref ?? `rule-${i}`,
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
    apply: (desired = file, mode = "apply") =>
      applyCacheSettings({ file: desired, imported, accountId, client, mode }),
  };
}

test("imports Monolith unchanged and respects object TTLs on data.jomcgi.dev", () => {
  assert.deepEqual(imported, { rules: [monolith] });
  assert.deepEqual(file.rules, [
    monolith,
    {
      description: "data.jomcgi.dev",
      ref: "jomcgi_site_data_cache_v1",
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

test("updates a rule bearing the apply's ownership ref without changing the import", async (t) => {
  const { apply } = await api(t, {
    rules: [monolith, { ...file.rules[1], enabled: false }],
  });
  await apply();
});

for (const mode of ["apply", "restore"]) {
  for (const [name, changes] of [
    ["matching settings", { ref: undefined }],
    ["different settings", { ref: undefined, enabled: false }],
    ["another writer's ref", { ref: "dashboard_data_rule" }],
  ]) {
    test(`${mode} preserves a pre-existing data rule with ${name} before any PUT`, async (t) => {
      const { apply, calls } = await api(t, {
        rules: [monolith, { ...file.rules[1], ...changes }],
      });
      await assert.rejects(
        apply(file, mode),
        /Refusing to adopt pre-existing rule "data.jomcgi.dev"/,
      );
      assert.deepEqual(
        calls.map(({ method }) => method),
        ["GET", "GET"],
      );
    });
  }
}

test("rejects a managed rule without an ownership ref before contacting Cloudflare", async (t) => {
  const { apply, calls } = await api(t);
  const desired = structuredClone(file);
  delete desired.rules[1].ref;
  await assert.rejects(apply(desired), /stable ownership ref/);
  assert.equal(calls.length, 0);
});

for (const [name, rules] of [
  ["disabled import", [{ ...monolith, enabled: false }]],
  [
    "changed TTL",
    [
      {
        ...monolith,
        action_parameters: {
          ...monolith.action_parameters,
          edge_ttl: { mode: "respect_origin" },
        },
      },
    ],
  ],
  ["missing import", []],
  ["changed order", file.rules.toReversed()],
]) {
  test(`refuses to change Monolith before PUT: ${name}`, async (t) => {
    const { apply, calls } = await api(t, { rules });
    await assert.rejects(
      apply(),
      /Refusing to change imported rule "Monolith"/,
    );
    assert.ok(calls.every(({ method }) => method === "GET"));
  });
}

test("restores the imported entrypoint before a chart downgrade and repeats the same body", async (t) => {
  const { apply, calls } = await api(t);
  await apply();
  await apply(file, "restore");
  await apply(file, "restore");
  const puts = calls.filter(({ method }) => method === "PUT");
  assert.equal(puts.length, 3);
  assert.equal(puts[0].body, JSON.stringify(file));
  assert.equal(puts[1].body, JSON.stringify(imported));
  assert.equal(puts[2].body, puts[1].body);
  // Each write includes successful readback through the real HTTP client.
  assert.deepEqual(
    calls.map(({ method }) => method),
    [
      "GET",
      "GET",
      "PUT",
      "GET",
      "GET",
      "GET",
      "PUT",
      "GET",
      "GET",
      "GET",
      "PUT",
      "GET",
    ],
  );
});

for (const [name, rules] of [
  [
    "new dashboard rule",
    [...file.rules, { ...monolith, description: "Dashboard only" }],
  ],
  ["edited data rule", [monolith, { ...file.rules[1], enabled: false }]],
  ["reordered rules", file.rules.toReversed()],
]) {
  test(`restore refuses to overwrite ${name}`, async (t) => {
    const { apply, calls } = await api(t, { rules });
    await assert.rejects(apply(file, "restore"), /Refusing/);
    assert.ok(calls.every(({ method }) => method === "GET"));
  });
}

test("restore fails if Cloudflare does not return the imported entrypoint", async (t) => {
  const { apply } = await api(t, {
    rules: file.rules,
    afterPut: () => file.rules,
  });
  await assert.rejects(apply(file, "restore"), /verification failed/);
});

test("rejects an edited import in the desired file before contacting Cloudflare", async (t) => {
  const { apply, calls } = await api(t);
  await assert.rejects(
    apply({ rules: [{ ...monolith, enabled: false }, file.rules[1]] }),
    /retain the imported rules unchanged/,
  );
  assert.equal(calls.length, 0);
});

for (const [name, afterPut] of [
  ["settings", (rules) => [{ ...rules[0], enabled: false }, rules[1]]],
  ["order", (rules) => rules.toReversed()],
  ["missing rule", (rules) => rules.slice(0, 1)],
  [
    "ownership ref",
    (rules) => [rules[0], { ...rules[1], ref: "another_writer" }],
  ],
  [
    "missing ownership ref",
    (rules) => [rules[0], { ...rules[1], ref: undefined }],
  ],
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
