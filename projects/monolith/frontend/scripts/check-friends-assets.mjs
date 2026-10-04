import { createServer } from "node:http";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { mkdir, writeFile } from "node:fs/promises";

// Built-browser regression, also run by factory-mobile-preview.yml. Run after the normal production vite build:
//   PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs CHROMIUM_PATH=/path/to/chromium \
//     node projects/monolith/frontend/scripts/check-friends-assets.mjs
// Requires Helm and Python with PyYAML (HELM_BIN/PYTHON may override their paths).
// Installs nothing, changes no cluster state, and contacts no production site.
// Auth is an explicit local test double, NOT verification of Envoy/OIDC crypto.
const { chromium } = await import(
  process.env.PLAYWRIGHT_MODULE || "playwright"
);
const root = fileURLToPath(new URL("../", import.meta.url));
const repo = path.resolve(root, "../../..");
const rendered = execFileSync(
  process.env.HELM_BIN || "helm",
  [
    "template",
    "monolith",
    path.join(repo, "projects/monolith/chart"),
    "-f",
    path.join(repo, "projects/monolith/deploy/values.yaml"),
    "-f",
    path.join(repo, "projects/monolith/deploy/values-gke.yaml"),
  ],
  { encoding: "utf8" },
);
const docs = JSON.parse(
  execFileSync(
    process.env.PYTHON || "python3",
    [
      "-c",
      'import json,sys,yaml; print(json.dumps([d for d in yaml.safe_load_all(sys.stdin) if d and d["kind"] in ["HTTPRoute","SecurityPolicy"]]))',
    ],
    { input: rendered, encoding: "utf8" },
  ),
);
const dispatch = docs.find(
  (d) => d.metadata.name === "monolith-friends-assets",
);
const records = [];
let handler, origin;
const server = createServer(async (req, res) => {
  const url = new URL(req.url, origin);
  const path = url.pathname;
  res.on("finish", () =>
    records.push({
      path,
      status: res.statusCode,
      referer: req.headers.referer || null,
    }),
  );
  const json = (value) => {
    res.setHeader("content-type", "application/json");
    res.end(JSON.stringify(value));
  };
  if (path === "/api/grimoire/lobby")
    return json({
      user: { display_name: "Local fixture", email: "fixture@example.invalid" },
      campaigns: [],
      invitations: [],
      can_administer_accounts: false,
    });
  if (path === "/api/grimoire/campaigns") return json([]);
  if (path === "/api/moving/state")
    return json({
      viewer: "fixture@example.invalid",
      tasks: [],
      spans: [],
      milestones: [],
      roles: [],
      collisions: [],
      progress: 0,
    });
  if (path === "/otel/v1/traces") {
    res.statusCode = 204;
    return res.end();
  }
  if (path === "/_app" || path.startsWith("/_app/")) {
    // Use rendered matches/targets, substituting only the local test origin.
    const referer = (req.headers.referer || "").replace(
      origin,
      "https://friends.jomcgi.dev",
    );
    const rule = dispatch.spec.rules.find((r) =>
      new RegExp(r.matches[0].headers[0].value).test(referer),
    );
    if (!rule) {
      res.statusCode = 404;
      return res.end("No dispatch match");
    }
    const redirect = rule.filters.find(
      (f) => f.type === "RequestRedirect",
    ).requestRedirect;
    for (const h of rule.filters.find(
      (f) => f.type === "ResponseHeaderModifier",
    ).responseHeaderModifier.set)
      res.setHeader(h.name, h.value);
    res.setHeader(
      "location",
      origin +
        redirect.path.replacePrefixMatch +
        path.slice("/_app".length) +
        url.search,
    );
    res.statusCode = 302;
    return res.end();
  }
  const app = path.match(/^\/(moving|grimoire)(?:\/|$)/)?.[1];
  if (!app) {
    res.statusCode = 404;
    return res.end();
  }
  // Explicit test double for the edge auth decision, not Envoy/OIDC evidence.
  const cookies = new Map(
    (req.headers.cookie || "").split(";").map((s) => s.trim().split("=")),
  );
  const policy = docs.find(
    (d) => d.kind === "SecurityPolicy" && d.metadata.name === `monolith-${app}`,
  ).spec;
  if (cookies.get(policy.oidc.cookieNames.idToken) !== `opaque-test-${app}`) {
    res.statusCode = 401;
    return res.end("Fixture session required");
  }
  const route = docs.find(
    (d) => d.kind === "HTTPRoute" && d.metadata.name === `monolith-${app}`,
  );
  const rule = route.spec.rules.find(
    (r) => r.matches[0].path.value === `/${app}/_app`,
  );
  if (path === `/${app}/_app` || path.startsWith(`/${app}/_app/`)) {
    const rewrite = rule.filters.find(
      (f) => f.type === "URLRewrite",
    ).urlRewrite;
    req.url =
      rewrite.path.replacePrefixMatch +
      path.slice(`/${app}/_app`.length) +
      url.search;
    for (const h of rule.filters.find(
      (f) => f.type === "ResponseHeaderModifier",
    ).responseHeaderModifier.set)
      res.setHeader(h.name, h.value);
  }
  const writeHead = res.writeHead;
  const protectedAsset = path.startsWith(`/${app}/_app/`);
  res.writeHead = function (...args) {
    if (protectedAsset) {
      for (const h of rule.filters.find(
        (f) => f.type === "ResponseHeaderModifier",
      ).responseHeaderModifier.set)
        this.setHeader(h.name, h.value);
    }
    return writeHead.apply(this, args);
  };
  handler(req, res);
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
origin = `http://friends.localhost:${server.address().port}`;
process.env.API_BASE = `http://127.0.0.1:${server.address().port}`;
({ handler } = await import(`${root}/dist/handler.js`));
const browser = await chromium.launch({
  ...(process.env.CHROMIUM_PATH
    ? { executablePath: process.env.CHROMIUM_PATH }
    : {}),
  headless: true,
  args: ["--no-sandbox"],
});
const output = path.resolve(
  process.env.FRIENDS_ASSETS_OUTPUT || "friends-assets-evidence",
);
await mkdir(output, { recursive: true });
const results = [];
try {
  for (const app of ["grimoire", "moving"]) {
    for (const sessions of [[app], ["grimoire", "moving"]]) {
      const context = await browser.newContext();
      await context.addCookies(
        sessions.map((name) => ({
          name: `${name}-id-token`,
          value: `opaque-test-${name}`,
          url: origin,
        })),
      );
      const page = await context.newPage();
      await page.route("**/*", (route) =>
        new URL(route.request().url()).origin === origin
          ? route.continue()
          : route.abort(),
      ); // never visit external services
      const errors = [];
      const responses = [];
      page.on("pageerror", (e) => errors.push(e.message));
      page.on("response", (r) => {
        if (r.url().includes("/_app/"))
          responses.push({
            url: r.url(),
            status: r.status(),
            type: r.headers()["content-type"],
          });
      });
      assert.equal(
        (
          await page.goto(`${origin}/${app}`, { waitUntil: "networkidle" })
        ).status(),
        200,
      );
      await page.locator(`.${app}`).first().waitFor();
      const style = await page
        .locator(`.${app}`)
        .first()
        .evaluate(
          (el, app) =>
            getComputedStyle(el)
              .getPropertyValue(
                app === "grimoire" ? "--grim-accent" : "--cream",
              )
              .trim(),
          app,
        );
      assert.ok(style, `${app} stylesheet applied`);
      assert.ok(
        responses.some((r) => r.status === 200 && r.type?.includes("text/css")),
      );
      assert.ok(
        responses.some(
          (r) => r.status === 200 && r.type?.includes("javascript"),
        ),
      );
      assert.ok(
        responses
          .filter((r) => r.status === 200)
          .every((r) => new URL(r.url).pathname.startsWith(`/${app}/_app/`)),
      );
      if (app === "grimoire") {
        await page
          .getByRole("link", { name: "Character sheets", exact: true })
          .click();
        await page.waitForURL("**/grimoire/sheets");
        await page.waitForLoadState("networkidle");
        await page.goBack();
        await page.waitForLoadState("networkidle");
      }
      assert.deepEqual(errors, []);
      const assets = responses.filter((r) => r.status === 200);
      results.push({
        app,
        sessions,
        appliedStyle: style,
        css: assets.filter((r) => r.type?.includes("text/css")).length,
        javascript: assets.filter((r) => r.type?.includes("javascript")).length,
        pageErrors: errors,
      });
      await page.screenshot({
        path: path.join(output, `${app}-${sessions.join("-")}.png`),
        fullPage: true,
      });
      await context.close();
    }
  }
  const request = await browser.newContext();
  const asset = records
    .find(
      (r) => r.path.startsWith("/grimoire/_app/") && r.path.endsWith(".css"),
    )
    .path.replace("/grimoire", "");
  for (const referer of [
    "",
    origin + "/grimoireevil",
    "https://evil.invalid/grimoire",
  ]) {
    const r = await request.request.get(origin + asset, {
      headers: referer ? { Referer: referer } : {},
      maxRedirects: 0,
    });
    assert.equal(r.status(), 404);
  }
  const redirected = await request.request.get(origin + asset + "?fixture=1", {
    headers: { Referer: origin + "/grimoire?next=https://evil.invalid/" },
    maxRedirects: 0,
  });
  assert.equal(redirected.status(), 302);
  assert.equal(
    redirected.headers().location,
    origin + "/grimoire" + asset + "?fixture=1",
  );
  assert.equal(redirected.headers()["cache-control"], "no-store");
  for (const app of ["moving", "grimoire"]) {
    assert.equal(
      (await request.request.get(origin + "/" + app + asset)).status(),
      401,
    );
  }
  await request.addCookies([
    { name: "moving-id-token", value: "opaque-test-moving", url: origin },
  ]);
  assert.equal(
    (await request.request.get(origin + "/grimoire" + asset)).status(),
    401,
  );
  assert.equal(
    (await request.request.get(origin + "/moving" + asset)).status(),
    200,
  );
  await request.close();

  console.log(
    JSON.stringify(
      {
        results,
        totalRequests: records.length,
        auth: "Fixture-only independent-lane deny/allow checks passed; real Envoy OIDC untested.",
      },
      null,
      2,
    ),
  );
} finally {
  await writeFile(
    path.join(output, "network.json"),
    JSON.stringify(
      {
        scope:
          "Local built-adapter browser/route test. Auth is an explicit test double, not live OIDC verification.",
        sha: process.env.FACTORY_PREVIEW_SHA || null,
        results,
        requests: records,
      },
      null,
      2,
    ),
  );
  await browser.close();
  server.close();
}
