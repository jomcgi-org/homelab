import { fileURLToPath } from "node:url";
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  writeFileSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomBytes } from "node:crypto";
import { gzipSync } from "node:zlib";
import {
  checkFreshness,
  checkHeaders,
  client,
  internalLinks,
  JUNK_PATH,
  tier1,
} from "./tier1.mjs";
import { snapshotTrips } from "../data/trips.mjs";
import {
  fixtureObject,
  matchRedirect,
  redirectRules,
  serveRelease,
} from "./release-fixture.mjs";
import { checkReleasePage, enabledSections } from "./tier1.mjs";
import {
  makeSummary,
  stampSummary,
  SUMMARY_END,
  SUMMARY_START,
} from "../data/stars-summary.mjs";
import {
  compressed,
  css,
  headers,
  headersFor,
  html,
  respond,
  serve,
} from "./fixture.mjs";
import { scopeFor } from "./scope.mjs";
import { parseHeaders } from "./headers.mjs";
import { snapshot } from "../data/snapshot.mjs";

test("production audits only Worker routes, including its scoped 404", async (t) => {
  const scope = scopeFor("prod");
  const dist = mkdtempSync(join(tmpdir(), "site-check-prod-"));
  t.after(() => rmSync(dist, { recursive: true, force: true }));
  mkdirSync(join(dist, "assets"));
  const notFound = html.replace("Readable page", "This page isn't here.");
  // Since HOME-206 the Worker serves the homepage, so the release pages link
  // to a path that stays off the Worker (the Scotland connector).
  const hubPath = "/scotland/mcp";
  assert.equal(scope.owns(hubPath), false);
  const page = html.replace('href="/"', `href="${hubPath}"`);
  for (const [name, body] of Object.entries({
    "index.html": page,
    "cv.html": page,
    "404.html": notFound,
    "assets/style.css": css,
    "assets/app.js": "",
  }))
    writeFileSync(join(dist, name), body);
  for (const failure of [null, "page", "404", "freshness", "link"]) {
    await t.test(failure ?? "hub and Worker coexist", async () => {
      const requests = [];
      const server = await serve((req, res) => {
        requests.push(req.url);
        if (req.url === hubPath) {
          // Hub HTML and headers differ from this release, but linked pages
          // must still be reachable. No Worker CSP/byte assertions apply.
          res.writeHead(failure === "link" ? 503 : 200, {
            "content-type": "text/html",
          });
          res.end("<h1>Existing hub page</h1>");
        } else if (req.url === "/") {
          respond(res, page);
        } else if (req.url === "/cv") {
          respond(res, failure === "page" ? "old CV" : page);
        } else if (scope.junkPaths.includes(req.url)) {
          respond(
            res,
            failure === "404" ? "hub 404" : notFound,
            "text/html",
            {},
            404,
          );
        } else if (req.url === "/cv/latest.json") {
          respond(
            res,
            JSON.stringify({
              generatedAt:
                failure === "freshness" ? "invalid" : new Date().toISOString(),
            }),
            "application/json",
          );
        } else if (req.url === "/assets/style.css") {
          respond(res, css, "text/css");
        } else if (req.url === "/assets/app.js") {
          respond(res, "", "text/javascript");
        } else {
          res.writeHead(500);
          res.end("unexpected request outside the Worker routes");
        }
      });
      const http = client(server.origin);
      try {
        const run = () =>
          tier1({
            origin: server.origin,
            dist,
            preview: false,
            get: http.get,
            scope: scopeFor("prod"),
            freshness: ["/cv/latest.json", `${hubPath}/latest.json`].map(
              (path) => ({
                path,
                timestampField: "generatedAt",
                maxAgeSeconds: 60,
              }),
            ),
          });
        if (failure) {
          await assert.rejects(
            run(),
            {
              page: /deployed page differs/,
              404: /deployed page differs/,
              freshness: /missing\/invalid/,
              link: /internal link/,
            }[failure],
          );
        } else {
          assert.deepEqual(
            (await run()).map(({ path }) => path).sort(),
            ["/", "/cv", ...scope.junkPaths].sort(),
          );
          assert.ok(requests.includes(hubPath), "hub links are still checked");
          assert.ok(requests.includes("/cv/latest.json"));
          assert.ok(!requests.includes(`${hubPath}/latest.json`));
          assert.ok(
            !requests.includes(JUNK_PATH),
            "the hub's 404 is not audited",
          );
        }
      } finally {
        await http.close();
        await server.close();
      }
    });
  }
});

test("Node fetch counts compressed wire bytes without Content-Length and fences redirects", async () => {
  let escaped = false;
  const external = await serve((req, res) => {
    escaped = true;
    res.end("unexpected");
  });
  const body = "a".repeat(20_000);
  const server = await serve((req, res) => {
    if (req.url === "/redirect") {
      res.writeHead(302, { location: external.origin });
      res.end();
    } else compressed(res, body, "text/javascript");
  });
  const http = client(server.origin);
  try {
    const response = await http.get("/");
    assert.equal(response.body.toString(), body);
    assert.equal(response.bytes, gzipSync(body).length);
    await assert.rejects(http.get("/redirect"), /off-origin redirect/);
    await assert.rejects(http.get(external.origin), /off-origin request/);
    assert.equal(escaped, false);
  } finally {
    await http.close();
    await server.close();
    await external.close();
  }
});

test("deployed pages, 404, headers, links, assets and the transfer budget fail closed", async (t) => {
  const dist = mkdtempSync(join(tmpdir(), "site-check-"));
  mkdirSync(join(dist, "assets"));
  const notFound = html.replace("Readable page", "This page isn't here.");
  const script = "console.log('ready')";
  for (const [name, data] of Object.entries({
    "index.html": html,
    "404.html": notFound,
    "assets/style.css": css,
    "assets/app.js": script,
  })) {
    writeFileSync(join(dist, name), data);
  }
  try {
    for (const failure of [
      null,
      "status",
      "404",
      "release",
      "csp",
      "robots",
      "cache",
      "link",
      "budget",
      "freshness",
    ]) {
      await t.test(failure ?? "passing release", async () => {
        const pageHtml =
          failure === "link"
            ? html.replace('href="/"', 'href="/missing"')
            : html;
        writeFileSync(join(dist, "index.html"), pageHtml);
        const server = await serve((req, res) => {
          const extra =
            failure === "csp"
              ? { "content-security-policy": "default-src *" }
              : failure === "robots"
                ? { "x-robots-tag": "index" }
                : failure === "cache"
                  ? { "cache-control": "no-cache" }
                  : {};
          if (req.url === "/hikes/latest.json") {
            respond(
              res,
              JSON.stringify({
                generatedAt: new Date(
                  Date.now() - (failure === "freshness" ? 600_000 : 0),
                ).toISOString(),
              }),
              "application/json",
            );
          } else if (req.url === "/assets/app.js") {
            compressed(
              res,
              failure === "budget" ? randomBytes(200_000) : script,
              "text/javascript",
            );
          } else if (req.url === "/assets/style.css") {
            respond(res, css, "text/css", extra);
          } else if (req.url === "/") {
            respond(
              res,
              failure === "release" ? "old release" : pageHtml,
              "text/html",
              extra,
              failure === "status" ? 503 : 200,
            );
          } else {
            respond(
              res,
              failure === "404" ? html : notFound,
              "text/html",
              extra,
              404,
            );
          }
        });
        const http = client(server.origin);
        try {
          const run = () =>
            tier1({
              origin: server.origin,
              dist,
              preview: true,
              freshness: [
                {
                  path: "/hikes/latest.json",
                  timestampField: "generatedAt",
                  maxAgeSeconds: 300,
                },
              ],
              get: http.get,
            });
          if (failure) {
            const reason = {
              status: /HTTP status/,
              404: /deployed page differs/,
              release: /deployed page differs/,
              csp: /CSP/,
              robots: /preview noindex/,
              cache: /public assets/,
              link: /internal link/,
              budget: /JS transfer/,
              freshness: /stale data/,
            }[failure];
            await assert.rejects(run(), reason);
          } else {
            const pages = await run();
            assert.deepEqual(
              pages.map((p) => p.path).sort(),
              ["/", JUNK_PATH].sort(),
            );
          }
        } finally {
          writeFileSync(join(dist, "index.html"), html);
          await http.close();
          await server.close();
        }
      });
    }
  } finally {
    rmSync(dist, { recursive: true, force: true });
  }
});

test("tier 1 reads Trips data only while the section is enabled", async (t) => {
  const dist = mkdtempSync(join(tmpdir(), "site-check-trips-"));
  t.after(() => rmSync(dist, { recursive: true, force: true }));
  mkdirSync(join(dist, "assets"));
  const notFound = html.replace("Readable page", "This page isn't here.");
  for (const [name, body] of Object.entries({
    "index.html": html,
    "trips.html": html,
    "404.html": notFound,
    "assets/style.css": css,
    "assets/app.js": "",
  }))
    writeFileSync(join(dist, name), body);
  // Like dev, this scope owns every path, so only the sources flag decides.
  const scope = { owns: () => true, junkPaths: [JUNK_PATH] };
  for (const enabled of [false, true]) {
    await t.test(enabled ? "enabled" : "disabled", async () => {
      const requests = [];
      const server = await serve((req, res) => {
        requests.push(req.url);
        if (req.url === "/") respond(res, html);
        else if (req.url === JUNK_PATH)
          respond(res, notFound, "text/html", {}, 404);
        else if (req.url === "/assets/style.css") respond(res, css, "text/css");
        else if (req.url === "/assets/app.js")
          respond(res, "", "text/javascript");
        else {
          res.writeHead(404);
          res.end("no such object");
        }
      });
      const http = client(server.origin);
      try {
        const run = () =>
          tier1({
            origin: server.origin,
            dist,
            preview: false,
            get: http.get,
            scope,
            sources: { sections: { trips: { enabled } } },
            freshness: [
              {
                path: "/data/trips/latest.json",
                timestampField: "generatedAt",
                maxAgeSeconds: 60,
              },
            ],
          });
        if (enabled) {
          await assert.rejects(run(), /\/data\/trips\/latest\.json/);
          assert.ok(requests.includes("/data/trips/latest.json"));
        } else {
          await run();
          assert.deepEqual(
            requests.filter((url) => url.startsWith("/data/trips/")),
            [],
          );
        }
      } finally {
        await http.close();
        await server.close();
      }
    });
  }
});

test("a disabled section gets no page or freshness check", () => {
  assert.deepEqual(
    [
      ...enabledSections({
        sections: {
          stars: {},
          trips: { enabled: false },
          hikes: { enabled: true },
        },
      }),
    ].sort(),
    ["hikes", "stars"],
  );
});

test("link parsing decodes HTML entities and keeps only the stage origin", () => {
  assert.deepEqual(
    internalLinks(
      '<a href="/x?a=1&amp;b=2">x</a><a href="https://elsewhere.test">elsewhere</a>',
      "https://next.jomcgi.dev/",
    ).map(String),
    ["https://next.jomcgi.dev/x?a=1&b=2"],
  );
});

test("freshness rejects stale, missing, invalid and future timestamps", () => {
  const now = Date.now();
  const entry = {
    path: "/hikes/latest.json",
    timestampField: "generatedAt",
    maxAgeSeconds: 300,
  };
  checkFreshness(
    { generatedAt: new Date(now - 10_000).toISOString() },
    entry,
    now,
  );
  for (const generatedAt of [
    undefined,
    "invalid",
    new Date(now - 301_000).toISOString(),
    new Date(now + 61_000).toISOString(),
  ]) {
    assert.throws(() => checkFreshness({ generatedAt }, entry, now));
  }
});

test("preview requires noindex; production can be indexed", () => {
  const production = new Headers(headers);
  production.delete("x-robots-tag");
  checkHeaders(production, { preview: false });
  assert.throws(
    () => checkHeaders(production, { preview: true }),
    /preview noindex/,
  );
});

test("a stamped hikes summary passes; an unstamped or altered page fails", () => {
  const slot = (body) =>
    `<main><!--hikes-summary:start-->${body}<!--hikes-summary:end--></main>`;
  const release = Buffer.from(slot("<p>build</p>"));
  const published =
    "<p>Data as of <time>t</time></p><p>1 Scottish walks in the published forecast. Explore walks.</p>";
  checkReleasePage(Buffer.from(slot(published)), release, "/hikes");
  assert.throws(
    () => checkReleasePage(release, release, "/hikes"),
    /hikes summary/,
  );
  assert.throws(
    () =>
      checkReleasePage(
        Buffer.from(`<p>x</p>${slot(published)}`),
        release,
        "/hikes",
      ),
    /differs/,
  );
});

test("CSP cannot be weakened by a subdirective or a duplicate", () => {
  for (const suffix of [
    "; script-src-elem * 'unsafe-inline'",
    "; script-src 'self'",
  ]) {
    const modified = new Headers(headers);
    modified.set(
      "content-security-policy",
      headers["content-security-policy"] + suffix,
    );
    assert.throws(() => checkHeaders(modified, { preview: true }), /CSP/);
  }
});

test("production wildcard routes include query URLs and retired-page 404s", () => {
  const scope = scopeFor("prod");
  assert.ok(scope.owns("/cv?ref=test"));
  assert.ok(scope.junkPaths.includes(`/cv${JUNK_PATH.slice(1)}`));
  for (const retired of ["/app/firecracker", "/artifact", "/ember", "/notes"]) {
    assert.ok(scope.junkPaths.includes(`${retired}${JUNK_PATH.slice(1)}`));
  }
  assert.ok(scope.junkPaths.every(scope.owns));
  const redirects = redirectRules(
    fileURLToPath(new URL("../web/public", import.meta.url)),
  );
  assert.equal(
    matchRedirect(redirects, `/app/llm-leaderboard${JUNK_PATH}`).status,
    301,
  );
  assert.ok(
    scope.junkPaths.includes(`/app/llm-leaderboard${JUNK_PATH.slice(1)}`),
  );
  for (const path of scope.junkPaths) {
    assert.equal(matchRedirect(redirects, path), undefined, path);
  }
  // Each section moves by adding its patterns, so derive the Worker's paths
  // from edge/routes.json and pin only what must stay off it: the connector,
  // the query variants of exact routes, the bare junk path and data.
  const routes = JSON.parse(
    readFileSync(new URL("../edge/routes.json", import.meta.url)),
  );
  for (const pattern of routes.patterns) {
    const path = pattern.slice(routes.zone.length);
    if (path.endsWith("*")) {
      const prefix = path.slice(0, -1);
      assert.ok(scope.owns(prefix), `${pattern} owns ${prefix}`);
      assert.ok(scope.owns(`${prefix}?ref=test`), `${pattern} keeps queries`);
    } else {
      assert.ok(scope.owns(path), `${pattern} owns ${path}`);
      assert.ok(!scope.owns(`${path}?ref=test`), `${pattern} is exact`);
    }
  }
  for (const path of [
    "/?ref=test",
    "/scotland/mcp",
    "/scotland/v1/tools",
    "/scotland?ref=test",
    "/slop?ref=test",
    JUNK_PATH,
    // A /data path that no section routes stays on the hub.
    "/data/__site-check-unrouted/latest.json",
  ]) {
    assert.equal(scope.owns(path), false, `${path} still belongs to the hub`);
  }
});

test("published snapshot freshness checks every section and detects a stopped refresh", async (t) => {
  const sources = JSON.parse(
    readFileSync(new URL("../data/sources.json", import.meta.url)),
  );
  const entries = JSON.parse(
    readFileSync(new URL("freshness.json", import.meta.url)),
  );
  assert.deepEqual(
    entries.map(({ path }) => path).sort(),
    Object.keys(sources.sections)
      .map((name) => `/data/${name}/latest.json`)
      .sort(),
  );
  const out = mkdtempSync(join(tmpdir(), "site-check-freshness-"));
  t.after(() => rmSync(out, { recursive: true, force: true }));
  const before = Date.now();
  const { trips, ...sections } = sources.sections;
  await snapshot({ sections }, fixtureObject, out);
  await snapshotTrips(trips, fixtureObject, out);
  for (const entry of entries) {
    const index = JSON.parse(
      readFileSync(join(out, entry.path.slice("/data/".length))),
    );
    assert.ok(Date.parse(index.generatedAt) >= before);
    assert.equal(index.asOf, "2020-11-17T12:00:00.000Z");
    checkFreshness(index, entry);
    assert.throws(
      () =>
        checkFreshness(index, entry, Date.parse(index.generatedAt) + 3_600_001),
      /stale data/,
    );
    assert.throws(
      () => checkFreshness({ ...index, generatedAt: undefined }, entry),
      /missing\/invalid/,
    );
  }
});

test("fixture headers keep asset caching, catalog caching and robots scoped to each route", async () => {
  const server = await serve((req, res) =>
    respond(res, "{}", "application/json", {}, 200, false),
  );
  const http = client(server.origin);
  try {
    const asset = await http.get("/assets/app.js");
    assert.match(asset.headers.get("cache-control"), /immutable/);
    assert.equal(asset.headers.get("x-robots-tag"), null);
    const catalog = await http.get("/scotland/marketplace.json");
    assert.match(catalog.headers.get("cache-control"), /max-age=0/);
    assert.doesNotMatch(catalog.headers.get("cache-control"), /immutable/);
    const slop = await http.get("/slop");
    assert.equal(slop.headers.get("x-robots-tag"), "noindex, nofollow");
    assert.equal(
      (await http.get("/scotland")).headers.get("cache-control"),
      null,
    );
    assert.equal(headersFor("/scotland", true)["x-robots-tag"], "noindex");
    assert.match(
      headersFor("/data/stars/v/sites-123.json", false)["cache-control"],
      /immutable/,
    );
    assert.equal(
      headersFor("/data/stars/latest.json", false)["cache-control"],
      undefined,
    );
  } finally {
    await http.close();
    await server.close();
  }
});

test("the release fixture applies _redirects like the Worker's asset server", () => {
  const dist = mkdtempSync(join(tmpdir(), "site-check-redirects-"));
  try {
    writeFileSync(
      join(dist, "_redirects"),
      "# comment\n/factory /factory/ 200\n/factory/* /factory/ 200\n/app/old /new 301\n",
    );
    const rules = redirectRules(dist);
    assert.deepEqual(matchRedirect(rules, "/factory"), {
      from: "/factory",
      to: "/factory/",
      status: 200,
    });
    assert.equal(matchRedirect(rules, "/factory/goals/1").to, "/factory/");
    assert.equal(matchRedirect(rules, "/app/old").status, 301);
    assert.equal(matchRedirect(rules, "/factoryx"), undefined);
    assert.equal(matchRedirect(rules, "/app/old/x"), undefined);
    assert.deepEqual(redirectRules(join(dist, "missing")), []);
  } finally {
    rmSync(dist, { recursive: true, force: true });
  }
});

test("a stamped stars page matches its release outside the summary markers", () => {
  const release = Buffer.from(
    `<main><h1>Stars</h1>${SUMMARY_START}<p>build</p>${SUMMARY_END}<p>release-trailer</p></main>`,
  );
  const summary = makeSummary(
    { sites: [] },
    Date.parse("2026-11-18T06:00:00Z"),
  );
  const stamped = Buffer.from(stampSummary(release.toString(), summary));
  checkReleasePage(stamped, release, "/stars");
  checkReleasePage(
    Buffer.from("<p>same</p>"),
    Buffer.from("<p>same</p>"),
    "/cv",
  );
  assert.throws(
    () =>
      checkReleasePage(
        Buffer.from("<p>old</p>"),
        Buffer.from("<p>new</p>"),
        "/cv",
      ),
    /deployed page differs/,
  );
  assert.throws(
    () =>
      checkReleasePage(
        Buffer.from(stamped.toString().replace("release-trailer", "edited")),
        release,
        "/stars",
      ),
    /deployed page differs/,
  );
  assert.throws(
    () =>
      checkReleasePage(
        Buffer.from(stamped.toString().replace("<h2>", "<h2>Injected ")),
        release,
        "/stars",
      ),
    /does not match its data/,
  );
  assert.throws(
    () =>
      checkReleasePage(
        Buffer.from(
          release.toString().replace("<p>build</p>", "<p>no data</p>"),
        ),
        release,
        "/stars",
      ),
    /has no data/,
  );
});

test("each page is held to its own _headers policy", () => {
  // Pinned: the site default, and the /hikes map policy that detaches it.
  const site = headersFor("/", false)["content-security-policy"];
  const hikes = headersFor("/hikes", false)["content-security-policy"];
  assert.match(site, /img-src 'self' data:;/);
  assert.match(hikes, /tiles\.openfreemap\.org/);
  assert.match(hikes, /worker-src 'self' blob:/);
  assert.equal(headersFor("/hikes", false)["content-security-policy"], hikes);
  const served = (csp, extra = {}) =>
    new Headers({
      ...headersFor("/", false),
      ...extra,
      "content-security-policy": csp,
    });
  checkHeaders(served(site), { preview: false, path: "/" });
  checkHeaders(
    served(hikes, {
      "permissions-policy": headersFor("/hikes", false)["permissions-policy"],
    }),
    { preview: false, path: "/hikes" },
  );
  // The wider map policy on another page, or the site policy on /hikes, fails.
  assert.throws(
    () => checkHeaders(served(hikes), { preview: false, path: "/" }),
    /CSP/,
  );
  assert.throws(
    () => checkHeaders(served(site), { preview: false, path: "/hikes" }),
    /CSP/,
  );
  // A _headers rule cannot lower the floor for any page.
  const weak = parseHeaders(
    "/*\n  Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'\n",
  );
  assert.throws(
    () =>
      checkHeaders(served(weak[0].headers["content-security-policy"]), {
        preview: false,
        path: "/",
        rules: weak,
      }),
    /_headers CSP/,
  );
});

test("the release fixture serves the storyboard manifest and every referenced object", async (t) => {
  const dist = mkdtempSync(join(tmpdir(), "storyboard-release-"));
  t.after(() => rmSync(dist, { recursive: true, force: true }));
  writeFileSync(
    join(dist, "hikes.html"),
    "<!--hikes-summary:start--><!--hikes-summary:end-->",
  );
  writeFileSync(join(dist, "404.html"), "Missing");
  const server = await serveRelease(dist, "dev");
  t.after(() => server.close());
  const base = new URL("/data/factory/", server.origin);
  const response = await fetch(new URL("manifest.json", base));
  assert.equal(response.status, 200);
  const manifest = await response.json();
  for (const path of [
    manifest.story,
    ...Object.values(manifest.goals),
    ...Object.values(manifest.issues),
    ...Object.values(manifest.sessions),
  ]) {
    const object = await fetch(new URL(path, base));
    assert.equal(object.status, 200, path);
    assert.equal((await object.json()).version, 1);
  }
});
