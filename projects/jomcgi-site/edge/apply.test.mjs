// Tests for apply.mjs against an in-memory zone. Run: node apply.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { apply, checkRules, equal, planRoutes, sameRule } from "./apply.mjs";

const read = (name) =>
  JSON.parse(readFileSync(new URL(name, import.meta.url), "utf8"));
const ROUTES = read("./routes.json");
const RULES = read("./cache-rules.json");
// The complete live entrypoint rules, read from the zone on 2026-11-17.
const LIVE = read("./testdata/live-cache-rules-2026-11-17.json").rules;
const SCRIPT = ROUTES.script;

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

test("changes nothing when a wanted route belongs to another Worker", async () => {
  const zone = fakeZone({
    routes: [
      { pattern: "jomcgi.dev/cv", script: "other-worker" },
      { pattern: "jomcgi.dev/old", script: SCRIPT },
    ],
    rules: [MONOLITH],
  });
  await assert.rejects(
    apply(zone.call, routesFile(["jomcgi.dev/cv"]), RULES),
    /belongs to another Worker/,
  );
  assert.deepEqual(zone.state.writes, []);
});

test("stops before any write when a live cache rule is not in the file", async () => {
  const dashboard = {
    id: "d",
    description: "made in the dashboard",
    expression: "true",
    action: "set_cache_settings",
    action_parameters: { cache: false },
    enabled: true,
  };
  const zone = fakeZone({ rules: [MONOLITH, dashboard] });
  await assert.rejects(
    apply(zone.call, ROUTES, RULES),
    /"made in the dashboard" is not in cache-rules.json/,
  );
  assert.deepEqual(zone.state.writes, []);
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

test("a live rule with an unknown top-level field is a difference", () => {
  assert.ok(
    !sameRule({ ...LIVE[0], logging: { enabled: false } }, RULES.rules[0]),
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
  assert.ok(log.includes(`create route jomcgi.dev/cv -> ${SCRIPT}`));
  assert.ok(log.includes(`put ${RULES.rules.length} cache rule(s)`));
});

test("rejects a rule with fields this script does not write", () => {
  assert.throws(
    () => checkRules([{ ...RULES.rules[0], ref: "x" }]),
    /must have exactly/,
  );
  const { enabled, ...missing } = RULES.rules[0];
  assert.throws(() => checkRules([missing]), /must have exactly/);
});

test("equal compares nested values exactly, in array order", () => {
  assert.ok(
    equal({ a: { b: 1, c: 2 }, d: [1, 2] }, { d: [1, 2], a: { c: 2, b: 1 } }),
  );
  assert.ok(!equal({ a: { b: 1, c: 2 } }, { a: { b: 1 } }));
  assert.ok(!equal({ a: { b: 1 } }, { a: { b: 1, c: 2 } }));
  assert.ok(!equal({ d: [2, 1] }, { d: [1, 2] }));
  assert.ok(!equal({}, { a: false }));
});

test("planRoutes ignores routes of other Workers", () => {
  const plan = planRoutes(
    [{ id: "1", pattern: "x/*", script: "other" }],
    [],
    SCRIPT,
  );
  assert.deepEqual(plan, { conflicts: [], create: [], remove: [] });
});
