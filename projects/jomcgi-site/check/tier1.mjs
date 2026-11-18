import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { gzipSync } from "node:zlib";
import { parse } from "parse5";
import { Agent } from "undici";
import { BUDGET_BYTES, htmlPages, initialScripts } from "../web/budget.mjs";
import { tripPages } from "../data/trips.mjs";
import { scopeFor } from "./scope.mjs";
import { headersFor, releaseHeaderRules } from "./headers.mjs";
import {
  SUMMARY_END,
  SUMMARY_START,
  summaryHtml,
} from "../data/stars-summary.mjs";

const HIKES_START = "<!--hikes-summary:start-->";
const HIKES_END = "<!--hikes-summary:end-->";

// Outside a stamped slot, a deployed page must equal this release.
function splitSlot(deployed, release, path, start, end) {
  const differs = `${path}: deployed page differs from this release`;
  const from = release.indexOf(start);
  const to = release.indexOf(end);
  if (from < 0 || to < from) return null;
  const head = release.subarray(0, from);
  const tail = release.subarray(to + end.length);
  assert.ok(
    deployed.length >= head.length + tail.length &&
      head.equals(deployed.subarray(0, head.length)) &&
      tail.equals(deployed.subarray(deployed.length - tail.length)),
    differs,
  );
  return deployed
    .subarray(head.length, deployed.length - tail.length)
    .toString();
}

// The deploy snapshot stamps a live summary between markers: the stars
// summary (stars.html, HOME-333) and the hikes summary (hikes.html,
// publishHikesSummary). The stars summary must be exactly what summaryHtml
// renders from the data it embeds. The hikes summary depends on the publish
// time, so it must be one well-formed slot with the published walk count.
export function checkReleasePage(deployed, release, path) {
  const hikes = splitSlot(deployed, release, path, HIKES_START, HIKES_END);
  if (hikes !== null) {
    assert.ok(
      hikes.startsWith(`${HIKES_START}<p>Data as of `) &&
        hikes.endsWith(HIKES_END) &&
        hikes.indexOf(HIKES_START, 1) < 0 &&
        /<p>\d+ Scottish walks in the published forecast\./.test(hikes),
      `${path}: hikes summary is not a published summary`,
    );
    return;
  }
  const stamped = splitSlot(
    deployed,
    release,
    path,
    SUMMARY_START,
    SUMMARY_END,
  );
  if (stamped === null) {
    assert.deepEqual(
      deployed,
      release,
      `${path}: deployed page differs from this release`,
    );
    return;
  }
  const data =
    /<script type="application\/json" id="stars-summary-data">(.*?)<\/script>/s.exec(
      stamped,
    )?.[1];
  assert.ok(data, `${path}: stamped summary has no data`);
  assert.equal(
    stamped,
    summaryHtml(JSON.parse(data)),
    `${path}: stamped summary does not match its data`,
  );
}
export { JUNK_PATH } from "./scope.mjs";

export function elements(html) {
  const result = [];
  function visit(node) {
    if (node.tagName) {
      result.push({
        tag: node.tagName,
        attrs: Object.fromEntries(
          node.attrs.map(({ name, value }) => [name, value]),
        ),
      });
    }
    for (const child of node.childNodes ?? []) visit(child);
  }
  visit(parse(html));
  return result;
}

export function internalLinks(html, base) {
  return elements(html)
    .filter(({ tag, attrs }) => tag === "a" && attrs.href)
    .map(({ attrs }) => new URL(attrs.href, base))
    .filter((url) => url.origin === new URL(base).origin);
}

const parseCsp = (value, label) => {
  const directives = (value ?? "")
    .split(";")
    .filter((part) => part.trim())
    .map((part) => part.trim().split(/\s+/))
    .map(([name, ...values]) => [name, values.sort().join(" ")]);
  const csp = new Map(directives);
  assert.equal(csp.size, directives.length, `duplicate CSP directives${label}`);
  return csp;
};

// Every page's policy must keep these, whatever its _headers rule sets.
const CSP_FLOOR = {
  "default-src": "'self'",
  "script-src": "'self'",
  "frame-ancestors": "'none'",
  "base-uri": "'none'",
  "form-action": "'none'",
  "object-src": "'none'",
};

const bundledRules = releaseHeaderRules();

export function checkHeaders(
  headers,
  { preview, asset = false, path = "/", rules = bundledRules },
) {
  // The expected headers are the release's _headers rules for this path.
  // Compare directive values, allowing differences in whitespace and order.
  const want = headersFor(rules, path, preview);
  const expected = parseCsp(want["content-security-policy"], " in _headers");
  const csp = parseCsp(headers.get("content-security-policy"), "");
  for (const [directive, value] of Object.entries(CSP_FLOOR)) {
    assert.equal(expected.get(directive), value, `_headers CSP ${directive}`);
  }
  for (const value of expected.values()) {
    assert.doesNotMatch(value, /'unsafe-|(^| )\*( |$)/, "_headers CSP");
  }
  for (const [directive, value] of expected) {
    assert.equal(csp.get(directive), value, `CSP ${directive}`);
  }
  for (const directive of csp.keys()) {
    assert.ok(
      expected.has(directive) ||
        ["report-uri", "report-to"].includes(directive),
      `unexpected CSP ${directive}`,
    );
  }
  if (want["permissions-policy"]) {
    assert.equal(
      headers.get("permissions-policy"),
      want["permissions-policy"],
      `${path}: Permissions-Policy`,
    );
  }
  assert.equal(headers.get("x-content-type-options"), "nosniff");
  assert.equal(
    headers.get("referrer-policy"),
    "strict-origin-when-cross-origin",
  );
  if (preview) {
    assert.match(
      headers.get("x-robots-tag") ?? "",
      /\bnoindex\b/i,
      "preview noindex",
    );
  }
  if (asset) {
    const cache = headers.get("cache-control") ?? "";
    assert.match(cache, /(?:^|,)\s*public\s*(?:,|$)/i, "public assets");
    assert.match(cache, /(?:^|,)\s*immutable\s*(?:,|$)/i, "immutable assets");
    assert.match(
      cache,
      /(?:^|,)\s*max-age=31536000\s*(?:,|$)/i,
      "asset lifetime",
    );
    assert.doesNotMatch(
      cache,
      /\b(?:no-store|no-cache|private)\b/i,
      "asset cache conflicts",
    );
  }
}

/** Node fetch decodes bodies. Count bytes at its dispatcher, before decoding. */
export function client(origin) {
  const base = new URL(origin);
  assert.equal(base.pathname, "/", "stage URL must be an origin");
  assert.ok(!base.username && !base.password && !base.search && !base.hash);
  assert.ok(
    base.protocol === "https:" ||
      (base.protocol === "http:" && base.hostname === "127.0.0.1"),
    "HTTPS is required outside loopback tests",
  );
  const agent = new Agent();
  const cache = new Map();
  async function get(path) {
    const url = new URL(path, base);
    assert.equal(url.origin, base.origin, `off-origin request: ${url}`);
    url.hash = "";
    if (cache.has(url.href)) return cache.get(url.href);
    const result = (async () => {
      let current = url;
      for (let redirects = 0; redirects < 6; redirects++) {
        let bytes = 0;
        const response = await fetch(current, {
          redirect: "manual",
          headers: {
            "accept-encoding": "gzip, br",
            "cache-control": "no-cache",
          },
          signal: AbortSignal.timeout(15_000),
          dispatcher: {
            dispatch(options, handler) {
              return agent.dispatch(
                options,
                new Proxy(handler, {
                  get(target, key) {
                    const value = Reflect.get(target, key);
                    if (key === "onData")
                      return (chunk) => {
                        bytes += chunk.length;
                        return value.call(target, chunk);
                      };
                    // Fetch's handler keeps response state on `this`.
                    return typeof value === "function"
                      ? value.bind(target)
                      : value;
                  },
                }),
              );
            },
          },
        });
        const body = Buffer.from(await response.arrayBuffer());
        if ([301, 302, 303, 307, 308].includes(response.status)) {
          assert.ok(
            response.headers.get("location"),
            `redirect without Location: ${current}`,
          );
          current = new URL(response.headers.get("location"), current);
          assert.equal(
            current.origin,
            base.origin,
            `off-origin redirect: ${current}`,
          );
          continue;
        }
        return {
          url: current.href,
          status: response.status,
          headers: response.headers,
          body,
          bytes,
        };
      }
      throw new Error(`too many redirects: ${url}`);
    })();
    cache.set(url.href, result);
    return result;
  }
  return { get, close: () => agent.close() };
}

export async function checkLinks(get, links) {
  for (const url of new Set(links.map(String))) {
    const response = await get(url);
    assert.equal(response.status, 200, `internal link ${url}`);
  }
}

export function checkFreshness(data, entry, now = Date.now()) {
  const timestamp = Date.parse(data[entry.timestampField]);
  assert.ok(
    Number.isFinite(entry.maxAgeSeconds) && entry.maxAgeSeconds > 0,
    "maxAgeSeconds must be positive",
  );
  assert.ok(
    Number.isFinite(timestamp),
    `${entry.path}: missing/invalid ${entry.timestampField}`,
  );
  assert.ok(
    timestamp <= now + 60_000,
    `${entry.path}: timestamp is in the future`,
  );
  assert.ok(
    now - timestamp <= entry.maxAgeSeconds * 1000,
    `${entry.path}: stale data`,
  );
}

function filesUnder(root, prefix = "") {
  return readdirSync(join(root, prefix), { withFileTypes: true }).flatMap(
    (entry) => {
      const path = `${prefix}/${entry.name}`;
      return entry.isDirectory() ? filesUnder(root, path) : [path];
    },
  );
}

const publishedFreshness = JSON.parse(
  readFileSync(new URL("freshness.json", import.meta.url)),
);

export async function tier1({
  origin,
  dist,
  preview,
  freshness = publishedFreshness,
  get,
  scope = scopeFor("dev"),
}) {
  const rules = releaseHeaderRules(dist);
  const files = htmlPages(dist);
  assert.ok(files.includes("index.html"), "missing homepage");
  assert.ok(files.includes("404.html"), "missing 404 page");
  const templates = new Set([
    "trips.html",
    "trip.html",
    "trip-day.html",
    "trip-timeline.html",
  ]);
  const expectedTrips = new Map();
  if (files.includes("trips.html") && scope.owns("/trips")) {
    const readJson = async (url) => {
      const response = await get(url);
      assert.equal(response.status, 200, url);
      return JSON.parse(response.body);
    };
    const latest = await readJson("/data/trips/latest.json");
    const readPayload = (url) => {
      assert.match(url, /^\/data\/trips\/v\/[a-f0-9]{64}\.json$/);
      return readJson(url);
    };
    const index = await readPayload(latest.files.index);
    const trips = [];
    for (const { slug } of index.trips)
      trips.push(await readPayload(latest.files.trips[slug]));
    for await (const { path, html } of tripPages(dist, {
      latest,
      index,
      trips,
    })) {
      expectedTrips.set(path, Buffer.from(html));
    }
  }
  const pages = files
    .filter((file) => !templates.has(file))
    .filter((file) => file !== "404.html")
    .map((file) => ({
      file,
      path: `/${file.replace(/index\.html$/, "").replace(/\.html$/, "")}`,
      status: 200,
    }))
    .filter(({ path }) => scope.owns(path));
  for (const path of expectedTrips.keys())
    pages.push({ path, status: 200, file: path.slice(1) + ".html" });
  assert.ok(pages.length, "no release pages are routed to this Worker");
  pages.push(
    ...scope.junkPaths.map((path) => ({ file: "404.html", path, status: 404 })),
  );
  const links = [];
  for (const page of pages) {
    const response = await get(page.path);
    assert.equal(response.status, page.status, `${page.path}: HTTP status`);
    assert.match(response.headers.get("content-type") ?? "", /text\/html/i);
    checkHeaders(response.headers, { preview, path: page.path, rules });
    checkReleasePage(
      response.body,
      expectedTrips.get(page.path) ?? readFileSync(join(dist, page.file)),
      page.path,
    );
    const html = response.body.toString();
    const { files, inline } = initialScripts(html);
    let bytes = inline.reduce((sum, body) => sum + gzipSync(body).length, 0);
    for (const file of files) {
      const script = await get(new URL(file, response.url));
      assert.equal(script.status, 200, `script ${file}`);
      assert.match(
        script.headers.get("content-type") ?? "",
        /(?:java|ecma)script/i,
        `script MIME type ${file}`,
      );
      bytes += script.bytes;
    }
    assert.ok(
      bytes <= BUDGET_BYTES,
      `${page.path}: JS transfer ${bytes} exceeds ${BUDGET_BYTES} bytes`,
    );
    links.push(...internalLinks(html, response.url));
    console.log(`tier 1 ${page.path}: ${response.status}, ${bytes} JS bytes`);
  }
  // Check all built assets, including CSS, fonts and lazy chunks.
  for (const path of filesUnder(dist).filter((path) =>
    path.startsWith("/assets/"),
  )) {
    assert.ok(scope.owns(path), `asset not routed to this Worker: ${path}`);
    const response = await get(path);
    assert.equal(response.status, 200, `asset ${path}`);
    checkHeaders(response.headers, { preview, asset: true, path, rules });
    assert.deepEqual(
      response.body,
      readFileSync(join(dist, path)),
      `${path}: deployed asset differs from this release`,
    );
  }
  await checkLinks(get, links);
  for (const entry of freshness.filter((entry) => scope.owns(entry.path))) {
    assert.match(
      entry.path,
      /^\/.*\/latest\.json$/,
      "freshness path must name a section's latest.json",
    );
    const response = await get(entry.path);
    assert.equal(response.status, 200, entry.path);
    checkFreshness(JSON.parse(response.body.toString()), entry);
  }
  return pages.map((page) => ({
    ...page,
    url: new URL(page.path, origin).href,
  }));
}
