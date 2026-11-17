// Makes zone jomcgi.dev match the files beside this script (HOME-192):
//   routes.json       every Workers route of one Worker on the zone
//   cache-rules.json  the rules of the zone's cache-settings entrypoint
// A route of that Worker that is not in the file is deleted, so removing a
// line rolls a section back to the hub. A live cache rule that is not in the
// file stops the run before any write, so a rule made in the dashboard is
// imported by a PR, never lost. Routes of other Workers are not touched.
//
// main.mjs runs it; this module only exports, so tests import it safely.

const API = "https://api.cloudflare.com/client/v4";
const PHASE = "http_request_cache_settings";
const RULE_KEYS = [
  "description",
  "expression",
  "action",
  "action_parameters",
  "enabled",
];

/** What to change so the Worker's routes are exactly `wanted`. */
export function planRoutes(live, wanted, script) {
  const byPattern = new Map(live.map((route) => [route.pattern, route]));
  return {
    conflicts: wanted.filter(
      (p) => byPattern.has(p) && byPattern.get(p).script !== script,
    ),
    create: wanted.filter((p) => !byPattern.has(p)),
    remove: live.filter(
      (route) => route.script === script && !wanted.includes(route.pattern),
    ),
  };
}

// Fields Cloudflare adds to every stored rule. Only these are ignored when a
// live rule is compared with the file; any other difference is a change.
const METADATA = new Set(["id", "ref", "version", "last_updated"]);

/** Deep equality of JSON values; object key order does not matter. */
export function equal(a, b) {
  if (
    a === null ||
    b === null ||
    typeof a !== "object" ||
    typeof b !== "object"
  ) {
    return a === b;
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const keys = Object.keys(a);
  return (
    keys.length === Object.keys(b).length &&
    keys.every((key) => Object.hasOwn(b, key) && equal(a[key], b[key]))
  );
}

/** True when a live rule is the file's rule plus Cloudflare's metadata only. */
export function sameRule(live, want) {
  const configured = Object.fromEntries(
    Object.entries(live).filter(([key]) => !METADATA.has(key)),
  );
  return equal(configured, want);
}

const ruleKey = (rule) => `${rule.description}\n${rule.expression}`;

/**
 * Live rules not in the file (by description and expression), and whether
 * the live list is exactly the file's list, in order. A setting added in the
 * dashboard to a known rule is a difference, so the PUT overwrites it.
 */
export function planCacheRules(live, wanted) {
  const known = new Set(wanted.map(ruleKey));
  return {
    unknown: live.filter((rule) => !known.has(ruleKey(rule))),
    same:
      live.length === wanted.length &&
      wanted.every((rule, i) => sameRule(live[i], rule)),
  };
}

/** Throws unless a file's rules carry exactly the fields this script writes. */
export function checkRules(rules) {
  for (const rule of rules) {
    const keys = Object.keys(rule).sort();
    if (keys.join() !== [...RULE_KEYS].sort().join()) {
      throw new Error(
        `cache rule "${rule.description}" must have exactly: ${RULE_KEYS.join(", ")}`,
      );
    }
  }
}

/** A Cloudflare API client; `fetch` is injectable for tests. */
export function client(token, fetchImpl = fetch) {
  return async (method, path, body) => {
    const response = await fetchImpl(`${API}${path}`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    return { status: response.status, data };
  };
}

async function ok(call, method, path, body) {
  const { status, data } = await call(method, path, body);
  if (status >= 300 || data.success === false) {
    const errors = (data.errors || []).map((e) => `${e.code} ${e.message}`);
    throw new Error(`${method} ${path}: ${status} ${errors.join("; ")}`);
  }
  return data.result;
}

async function liveRules(call, zone) {
  const path = `/zones/${zone}/rulesets/phases/${PHASE}/entrypoint`;
  const { status, data } = await call("GET", path);
  if (status === 404) return []; // The zone has no entrypoint yet.
  if (status >= 300) throw new Error(`GET ${path}: ${status}`);
  return data.result.rules || [];
}

/** Plans, checks, writes and reads back. Returns the log lines. */
export async function apply(
  call,
  routesFile,
  rulesFile,
  { dryRun = false } = {},
) {
  if (routesFile.zone !== rulesFile.zone)
    throw new Error("routes and rules name different zones");
  checkRules(rulesFile.rules);
  const log = [];
  const zones = await ok(
    call,
    "GET",
    `/zones?name=${encodeURIComponent(routesFile.zone)}`,
  );
  if (zones.length !== 1) throw new Error(`zone ${routesFile.zone} not found`);
  const zone = zones[0].id;
  const { script, patterns } = routesFile;

  const routes = planRoutes(
    await ok(call, "GET", `/zones/${zone}/workers/routes`),
    patterns,
    script,
  );
  const live = await liveRules(call, zone);
  const rules = planCacheRules(live, rulesFile.rules);
  const stop = [
    ...routes.conflicts.map((p) => `route ${p} belongs to another Worker`),
    ...rules.unknown.map(
      (r) =>
        `live cache rule "${r.description}" is not in cache-rules.json; import it first`,
    ),
  ];
  if (stop.length) throw new Error(`nothing changed:\n  ${stop.join("\n  ")}`);

  for (const p of routes.create) log.push(`create route ${p} -> ${script}`);
  for (const r of routes.remove) log.push(`delete route ${r.pattern}`);
  if (!rules.same) {
    rulesFile.rules.forEach((rule, i) => {
      if (!live[i] || !sameRule(live[i], rule)) {
        log.push(
          `cache rule ${i + 1} "${rule.description}" differs from the file`,
        );
      }
    });
    log.push(`put ${rulesFile.rules.length} cache rule(s)`);
  }
  if (!log.length) log.push("no change");
  if (dryRun) return log;

  for (const p of routes.create) {
    await ok(call, "POST", `/zones/${zone}/workers/routes`, {
      pattern: p,
      script,
    });
  }
  for (const r of routes.remove) {
    await ok(call, "DELETE", `/zones/${zone}/workers/routes/${r.id}`);
  }
  if (!rules.same) {
    await ok(
      call,
      "PUT",
      `/zones/${zone}/rulesets/phases/${PHASE}/entrypoint`,
      { rules: rulesFile.rules },
    );
  }

  const after = planRoutes(
    await ok(call, "GET", `/zones/${zone}/workers/routes`),
    patterns,
    script,
  );
  const afterRules = planCacheRules(
    await liveRules(call, zone),
    rulesFile.rules,
  );
  if (
    after.create.length ||
    after.remove.length ||
    after.conflicts.length ||
    !afterRules.same
  ) {
    throw new Error("read-back does not match the files");
  }
  log.push("read-back matches the files");
  return log;
}
