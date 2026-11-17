// Tests for apply.mjs against an in-memory zone. Run: node apply.test.mjs
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import {
  apply,
  checkRules,
  empty,
  matches,
  planRoutes,
  sameRule,
} from "./apply.mjs";

const read = (name) =>
  JSON.parse(readFileSync(new URL(name, import.meta.url), "utf8"));
const ROUTES = read("./routes.json");
const RULES = read("./cache-rules.json");
// The complete live entrypoint rules, read from the zone on 2026-11-17.
const LIVE = read("./testdata/live-cache-rules-2026-11-17.json").rules;
const SCRIPT = ROUTES.script;
const SCOTLAND_AND_SLOP_PATHS = [
  "/scotland",
  "/scotland/privacy",
  "/scotland/terms",
  "/scotland/support",
  "/scotland/marketplace.json",
  "/slop",
];

// A Worker route's * can span path separators. Check intersection with the
// whole protected subtree, including endpoints added to the connector later.
function canMatchPrefix(pattern, prefix) {
  const seen = new Set();
  function visit(p, s) {
    const key = `${p}:${s}`;
    if (seen.has(key)) return false;
    seen.add(key);
    if (s === prefix.length) return true;
    if (p === pattern.length) return false;
    if (pattern[p] === "*") return visit(p + 1, s) || visit(p, s + 1);
    return pattern[p] === prefix[s] && visit(p + 1, s + 1);
  }
  return visit(0, 0);
}

test("Scotland and slop own only exact static URLs", () => {
  for (const path of SCOTLAND_AND_SLOP_PATHS)
    assert.ok(ROUTES.patterns.includes(`jomcgi.dev${path}`));
  for (const pattern of ROUTES.patterns) {
    const route = pattern.replace(/^https?:\/\//, "");
    for (const prefix of [
      "jomcgi.dev/scotland/mcp",
      "jomcgi.dev/scotland/v1",
      "jomcgi.dev/slop/factory",
    ]) {
      assert.ok(
        !canMatchPrefix(route, prefix),
        `${pattern} could intercept ${prefix}`,
      );
    }
  }
});

test("the boundary check catches broad routes and future connector endpoints", () => {
  for (const pattern of [
    "jomcgi.dev/*",
    "jomcgi.dev/scotland*",
    "*jomcgi.dev/scotland/*",
    "jomcgi.dev/scotland/v*/future/endpoint",
  ]) {
    assert.ok(canMatchPrefix(pattern, "jomcgi.dev/scotland/v1/"));
  }
  assert.ok(canMatchPrefix("jomcgi.dev/slop/*", "jomcgi.dev/slop/factory"));
  assert.ok(!canMatchPrefix("jomcgi.dev/scotland", "jomcgi.dev/scotland/mcp"));
});

const result = (value) => ({
  status: 200,
  data: { success: true, result: value },
});

/** A fake zone. `extra` is what Cloudflare adds to each rule it stores. */
function fakeZone({
  routes = [],
  rules = null,
  extra = {},
  dropOnPut = false,
} = {}) {
  const state = {
    routes: routes.map((route, i) => ({ id: `r${i}`, ...route })),
    rules,
    writes: [],
  };
  let next = 100;
  const call = async (method, path, body) => {
    if (method !== "GET") state.writes.push(`${method} ${path}`);
    if (path.startsWith("/zones?name=")) return result([{ id: "z1" }]);
    if (path === "/zones/z1/workers/routes" && method === "GET")
      return result(state.routes);
    if (path === "/zones/z1/workers/routes" && method === "POST") {
      state.routes.push({ id: `r${next++}`, ...body });
      return result({});
    }
    if (path.startsWith("/zones/z1/workers/routes/") && method === "DELETE") {
      const id = path.split("/").pop();
      state.routes = state.routes.filter((route) => route.id !== id);
      return result({});
    }
    if (path.endsWith("/http_request_cache_settings/entrypoint")) {
      if (method === "GET") {
        return state.rules === null
          ? { status: 404, data: { success: false, errors: [{ code: 10003 }] } }
          : result({ rules: state.rules });
      }
      if (method === "PUT") {
        const stored = body.rules.map((rule, i) => ({
          id: `c${i}`,
          version: "2",
          ...rule,
          ...extra,
        }));
        state.rules = dropOnPut ? stored.slice(1) : stored;
        return result({});
      }
    }
    throw new Error(`unexpected ${method} ${path}`);
  };
  return { state, call };
}

const routesFile = (patterns) => ({ ...ROUTES, patterns });
const MONOLITH = { id: "m", version: "1", ...RULES.rules[0] };

// Cloudflare matches a Workers route against the whole URL, query string
// included. A pattern ending in `*` matches every suffix; any other pattern
// matches only that exact URL, so `jomcgi.dev/cv` misses `/cv?ref=x`.
// https://developers.cloudflare.com/workers/configuration/routing/routes/
function routeMatches(pattern, url) {
  return pattern.endsWith("*")
    ? url.startsWith(pattern.slice(0, -1))
    : url === pattern;
}
const served = (url) => ROUTES.patterns.some((p) => routeMatches(p, url));

test("the exact Scotland and slop exceptions leave queries and descendants on the hub", () => {
  for (const path of SCOTLAND_AND_SLOP_PATHS) {
    const url = `jomcgi.dev${path}`;
    assert.ok(served(url), path);
    assert.ok(ROUTES.exactBecause[url], `${path} needs an exact-match reason`);
    for (const suffix of ["?ref=x", "/future-page", "/future-page?ref=x"]) {
      assert.ok(!served(`${url}${suffix}`), `${path}${suffix}`);
    }
  }
});

// Paths that other services or the hub still own on jomcgi.dev.
const STAYS_ON_THE_HUB = [
  "jomcgi.dev/scotland/mcp",
  "jomcgi.dev/scotland/v1/walks",
  "jomcgi.dev/health",
  "jomcgi.dev/img/x/thumb/plain/s3://monolith-trips/a.jpg",
  "jomcgi.dev/functions/og-image",
  "jomcgi.dev/api/trips/trips",
  "jomcgi.dev/slop/factory",
  "jomcgi.dev/app/hikes",
  "jomcgi.dev/app/stars",
  "jomcgi.dev/app/grimoire",
  "jomcgi.dev/blog",
];

test("no route catches a path the hub or another service still owns", () => {
  assert.deepEqual(STAYS_ON_THE_HUB.filter(served), []);
});

test("routed pages are served with and without a query string", () => {
  for (const path of [
    "/cv",
    "/cv/",
    "/app/notes",
    "/app/notes/graph",
    "/notes",
    "/ember",
    "/ember/firecracker",
    "/app/firecracker",
    "/artifact/abc",
    "/assets/index-x.js",
  ]) {
    assert.ok(served(`jomcgi.dev${path}`), path);
    assert.ok(served(`jomcgi.dev${path}?ref=x`), `${path}?ref=x`);
  }
});

test("a pattern without a wildcard says why, since a query string bypasses it", () => {
  // Exact patterns miss `?query` URLs. Use one only where a wildcard would
  // catch a path that must stay elsewhere (the root, or /scotland beside the
  // connector's /scotland/mcp), and give the reason in routes.json.
  const exact = ROUTES.patterns.filter((p) => !p.endsWith("*"));
  assert.deepEqual(
    exact.filter((p) => !ROUTES.exactBecause?.[p]),
    [],
  );
  for (const p of Object.keys(ROUTES.exactBecause ?? {})) {
    assert.ok(
      exact.includes(p),
      `exactBecause names ${p}, which is not an exact pattern`,
    );
  }
});

test("the files are valid and import the live Monolith rule completely", () => {
  assert.equal(ROUTES.zone, RULES.zone);
  checkRules(RULES.rules);
  // The zone held one rule. Apart from Cloudflare's metadata, the file's
  // copy has exactly its settings, so the first PUT drops nothing.
  assert.equal(LIVE.length, 1);
  assert.deepEqual(
    Object.keys(LIVE[0])
      .filter((key) => !(key in RULES.rules[0]))
      .sort(),
    ["id", "last_updated", "ref", "version"],
  );
  assert.ok(sameRule(LIVE[0], RULES.rules[0]));
});

test("creates missing routes, deletes stale ones, and leaves other Workers alone", async () => {
  const zone = fakeZone({
    routes: [
      { pattern: "jomcgi.dev/cv", script: SCRIPT },
      { pattern: "jomcgi.dev/hikes*", script: SCRIPT },
      { pattern: "jomcgi.dev/other*", script: "other-worker" },
    ],
    rules: [MONOLITH],
  });
  const log = await apply(
    zone.call,
    routesFile(["jomcgi.dev/cv", "jomcgi.dev/stars"]),
    RULES,
  );
  assert.deepEqual(
    zone.state.routes.map((route) => `${route.pattern} ${route.script}`).sort(),
    [
      "jomcgi.dev/cv jomcgi-site",
      "jomcgi.dev/other* other-worker",
      "jomcgi.dev/stars jomcgi-site",
    ],
  );
  assert.ok(log.includes("delete route jomcgi.dev/hikes*"));
  assert.ok(log.includes("read-back matches the files"));
});

test("with routes off, deletes every route of the Worker and still applies the rules", async () => {
  const zone = fakeZone({
    routes: [
      ...ROUTES.patterns.map((pattern) => ({ pattern, script: SCRIPT })),
      { pattern: "jomcgi.dev/other*", script: "other-worker" },
    ],
    rules: [MONOLITH],
  });
  const log = await apply(zone.call, ROUTES, RULES, { serveRoutes: false });
  assert.deepEqual(
    zone.state.routes.map((route) => route.pattern),
    ["jomcgi.dev/other*"],
  );
  for (const pattern of ROUTES.patterns) {
    assert.ok(log.includes(`delete route ${pattern}`));
  }
  assert.ok(
    zone.state.rules.every((rule, i) => sameRule(rule, RULES.rules[i])),
  );
});

test("main refuses to run unless EDGE_SERVE_ROUTES is true or false", () => {
  const main = fileURLToPath(new URL("./main.mjs", import.meta.url));
  for (const value of [undefined, "", "yes", "TRUE"]) {
    // Keep the parent's environment: the rules_js node wrapper needs it.
    const env = { ...process.env, CLOUDFLARE_API_TOKEN: "unused" };
    delete env.EDGE_SERVE_ROUTES;
    if (value !== undefined) env.EDGE_SERVE_ROUTES = value;
    const run = spawnSync(process.execPath, [main], { env, encoding: "utf8" });
    assert.notEqual(run.status, 0, `EDGE_SERVE_ROUTES=${value}`);
    assert.match(run.stderr, /EDGE_SERVE_ROUTES must be "true" or "false"/);
  }
});

test("a route conflict blocks only the route writes, then the run fails", async () => {
  const zone = fakeZone({
    routes: [
      { pattern: "jomcgi.dev/cv", script: "other-worker" },
      { pattern: "jomcgi.dev/old", script: SCRIPT },
    ],
    rules: [MONOLITH],
  });
  await assert.rejects(
    apply(zone.call, routesFile(["jomcgi.dev/cv"]), RULES),
    /belongs to another Worker; no route was changed/,
  );
  assert.ok(!zone.state.writes.some((w) => w.includes("/workers/routes")));
  // The cache-rules half is independent and still applies.
  assert.ok(
    zone.state.rules.every((rule, i) => sameRule(rule, RULES.rules[i])),
  );
});

const DASHBOARD_RULE = {
  id: "d",
  description: "made in the dashboard",
  expression: "true",
  action: "set_cache_settings",
  action_parameters: { cache: false },
  enabled: true,
};

test("an unknown live cache rule blocks only the cache-rules write, then the run fails", async () => {
  const zone = fakeZone({ rules: [MONOLITH, DASHBOARD_RULE] });
  await assert.rejects(
    apply(zone.call, ROUTES, RULES),
    /"made in the dashboard" is not in cache-rules.json; no cache rule was changed/,
  );
  assert.ok(!zone.state.writes.some((w) => w.startsWith("PUT")));
  assert.deepEqual(zone.state.rules, [MONOLITH, DASHBOARD_RULE]);
});

test("rollback with routes off still deletes the routes when a dashboard cache rule exists", async () => {
  const zone = fakeZone({
    routes: ROUTES.patterns.map((pattern) => ({ pattern, script: SCRIPT })),
    rules: [MONOLITH, DASHBOARD_RULE],
  });
  await assert.rejects(
    apply(zone.call, ROUTES, RULES, { serveRoutes: false }),
    /not fully applied/,
  );
  assert.deepEqual(zone.state.routes, []);
  assert.deepEqual(zone.state.rules, [MONOLITH, DASHBOARD_RULE]);
});

test("sends no write when the zone already matches, with Cloudflare's metadata", async () => {
  const stored = RULES.rules.map((rule, i) => ({
    id: `c${i}`,
    ref: `c${i}`,
    version: "3",
    last_updated: "x",
    ...rule,
  }));
  const zone = fakeZone({
    routes: ROUTES.patterns.map((pattern) => ({ pattern, script: SCRIPT })),
    rules: stored,
  });
  const log = await apply(zone.call, ROUTES, RULES);
  assert.deepEqual(zone.state.writes, []);
  assert.deepEqual(log, ["no change", "read-back matches the files"]);
});

test("a rule Cloudflare returns with empty default fields still matches", async () => {
  const stored = RULES.rules.map((rule, i) => ({
    id: `c${i}`,
    ...rule,
    action_parameters: {
      ...rule.action_parameters,
      origin_error_page_passthru: false,
      cache_key: { ignore_query_strings_order: false },
    },
  }));
  const zone = fakeZone({
    routes: ROUTES.patterns.map((pattern) => ({ pattern, script: SCRIPT })),
    rules: stored,
  });
  assert.deepEqual(await apply(zone.call, ROUTES, RULES), [
    "no change",
    "read-back matches the files",
  ]);
  assert.deepEqual(zone.state.writes, []);
});

test("converges when Cloudflare adds empty defaults to the rules it stores", async () => {
  const zone = fakeZone({
    rules: [LIVE[0]],
    extra: { logging: { enabled: false } },
  });
  await apply(zone.call, ROUTES, RULES);
  const again = await apply(zone.call, ROUTES, RULES);
  assert.deepEqual(again, ["no change", "read-back matches the files"]);
});

test("overwrites a setting added in the dashboard to a known rule", async () => {
  const edited = {
    ...LIVE[0],
    action_parameters: {
      ...LIVE[0].action_parameters,
      browser_ttl: { mode: "override_origin", default: 60 },
    },
  };
  const zone = fakeZone({
    routes: ROUTES.patterns.map((pattern) => ({ pattern, script: SCRIPT })),
    rules: [edited],
  });
  const log = await apply(zone.call, ROUTES, RULES);
  assert.ok(log.includes('cache rule 1 "Monolith" differs from the file'));
  assert.ok(
    zone.state.writes.includes(
      "PUT /zones/z1/rulesets/phases/http_request_cache_settings/entrypoint",
    ),
  );
  assert.ok(
    zone.state.rules.every((rule, i) => sameRule(rule, RULES.rules[i])),
  );
});

test("a live rule with an unknown top-level setting is a difference", () => {
  assert.ok(
    !sameRule({ ...LIVE[0], logging: { enabled: true } }, RULES.rules[0]),
  );
  assert.ok(
    sameRule({ ...LIVE[0], logging: { enabled: false } }, RULES.rules[0]),
  );
});

test("writes the rules when the entrypoint does not exist yet", async () => {
  const zone = fakeZone({ rules: null, extra: { ref: "r" } });
  await apply(zone.call, ROUTES, RULES);
  assert.ok(
    zone.state.writes.includes(
      "PUT /zones/z1/rulesets/phases/http_request_cache_settings/entrypoint",
    ),
  );
  assert.equal(zone.state.rules.length, RULES.rules.length);
});

test("fails when the read-back does not match", async () => {
  const zone = fakeZone({ rules: [MONOLITH], dropOnPut: true });
  await assert.rejects(
    apply(zone.call, ROUTES, RULES),
    /read-back does not match/,
  );
});

test("a dry run plans and writes nothing", async () => {
  const zone = fakeZone({ rules: [MONOLITH] });
  const log = await apply(zone.call, ROUTES, RULES, { dryRun: true });
  assert.deepEqual(zone.state.writes, []);
  assert.ok(log.includes(`create route jomcgi.dev/cv* -> ${SCRIPT}`));
  assert.ok(log.includes(`put ${RULES.rules.length} cache rule(s)`));
});

test("a dry run that a route conflict would block writes nothing and fails with the plan", async () => {
  const zone = fakeZone({
    routes: [{ pattern: "jomcgi.dev/cv", script: "other-worker" }],
    rules: [MONOLITH],
  });
  await assert.rejects(
    apply(zone.call, routesFile(["jomcgi.dev/cv"]), RULES, { dryRun: true }),
    (error) =>
      /dry run is blocked/.test(error.message) &&
      /belongs to another Worker/.test(error.message) &&
      error.message.includes(`put ${RULES.rules.length} cache rule(s)`),
  );
  assert.deepEqual(zone.state.writes, []);
});

test("a dry run that an unknown cache rule would block writes nothing and fails with the plan", async () => {
  const zone = fakeZone({ rules: [MONOLITH, DASHBOARD_RULE] });
  await assert.rejects(
    apply(zone.call, ROUTES, RULES, { dryRun: true }),
    (error) =>
      /dry run is blocked/.test(error.message) &&
      /"made in the dashboard" is not in cache-rules.json/.test(
        error.message,
      ) &&
      error.message.includes(`create route jomcgi.dev/cv* -> ${SCRIPT}`),
  );
  assert.deepEqual(zone.state.writes, []);
});

test("rejects a rule with fields this script does not write", () => {
  assert.throws(
    () => checkRules([{ ...RULES.rules[0], ref: "x" }]),
    /must have exactly/,
  );
  const { enabled, ...missing } = RULES.rules[0];
  assert.throws(() => checkRules([missing]), /must have exactly/);
});

test("matches needs the file's values and allows only empty extras", () => {
  assert.ok(
    matches({ a: { b: 1, c: false }, d: [1, 2] }, { d: [1, 2], a: { b: 1 } }),
  );
  assert.ok(!matches({ a: { b: 1, c: 2 } }, { a: { b: 1 } }));
  assert.ok(!matches({ a: { b: 1, c: "x" } }, { a: { b: 1 } }));
  assert.ok(!matches({ a: { b: 1 } }, { a: { b: 1, c: false } }));
  assert.ok(!matches({ d: [2, 1] }, { d: [1, 2] }));
  assert.ok(!matches({ a: true }, { a: false }));
  assert.ok(empty({ x: false, y: { z: null, w: [] } }));
  assert.ok(!empty({ mode: "override_origin" }));
});

test("planRoutes ignores routes of other Workers", () => {
  const plan = planRoutes(
    [{ id: "1", pattern: "x/*", script: "other" }],
    [],
    SCRIPT,
  );
  assert.deepEqual(plan, { conflicts: [], create: [], remove: [] });
});
