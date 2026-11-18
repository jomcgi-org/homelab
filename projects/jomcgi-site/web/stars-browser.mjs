// Compiled-page acceptance: pnpm build && pnpm test:browser.
import assert from "node:assert/strict";
import { cp, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve } from "node:path";
import chrome, { inflate } from "@sparticuz/chromium";
import { chromium } from "playwright";
import { expect } from "playwright/test";
import { snapshot } from "../data/snapshot.mjs";
// Keep emitted metadata, redirects, script budget and structural checks too.
await import("./stars-built.mjs");
const root = await mkdtemp(join(tmpdir(), "stars-browser-"));
await cp(resolve(process.argv[2]), root, { recursive: true });
const now = Date.parse("2026-11-17T20:00:00Z");
const hour = (offset) => new Date(now + offset * 3_600_000).toISOString();
// Upstream total-hour order (8 before 7) differs from selected-night order.
const forecastHours = [
  [2, 3, 4, 5, 26, 27, 28, 29],
  [2, 3, 4, 5, 6, 7, 8],
];
const sites = Array.from({ length: 95 }, (_, i) => ({
  id: `site-${i + 1}`,
  name: `Site ${i + 1}`,
  lat: 55.5 + (i % 10) * 0.05,
  lon: -4 + Math.floor(i / 10) * 0.05,
  clear_dark_hours: forecastHours[i]?.length ?? (i === 94 ? 1 : 2),
  best_hours: (forecastHours[i] ?? (i === 94 ? [2] : [2, 26])).map(
    (offset) => ({
      time: hour(offset),
      cloud_area_fraction: 4,
      dark: true,
    }),
  ),
}));
const payloads = {
  "sites.json": { fetched_at: hour(-2), sites },
  "history-map.json": {
    sites: sites.map((site) => ({
      ...site,
      clear: Array.from({ length: 12 }, (_, m) => m + 1),
      dark: Array(12).fill(100),
    })),
  },
  "map.json": { sites },
};
const sources = JSON.parse(
  await readFile(new URL("../data/sources.json", import.meta.url)),
);
await snapshot(
  { sections: { stars: sources.sections.stars } },
  async (_bucket, key) => ({
    body: Buffer.from(JSON.stringify(payloads[key])),
    lastModified: new Date(now).toUTCString(),
  }),
  join(root, "data"),
);
const headers = await readFile(join(root, "_headers"), "utf8");
// Apply the matching CSP from the built headers to each response. A worker
// inherits the site rule unless its own asset rule replaces it.
const policies = headers.split(/\n(?=\S)/).flatMap((block) => {
  const policy = block.match(/^  Content-Security-Policy: (.+)/m)?.[1];
  if (!policy) return [];
  const pattern = block
    .split("\n")[0]
    .replaceAll(".", "\\.")
    .replaceAll("*", ".*");
  return [{ path: new RegExp(`^${pattern}$`), policy }];
});
const axePath = createRequire(import.meta.url).resolve("axe-core/axe.min.js");
const origin = "https://stars.test";
const mime = {
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
  ".json": "application/json",
};
let browser;
try {
  let executablePath = process.env.CHROMIUM_BIN;
  const env = { ...process.env };
  if (!executablePath) {
    const bin = join(
      dirname(createRequire(import.meta.url).resolve("@sparticuz/chromium")),
      "../bin",
    );
    await inflate(join(bin, "al2023.tar.br"));
    executablePath = await chrome.executablePath();
    // The archive hard-codes /tmp/fonts; Bazel supplies its own TMPDIR.
    const xml = (s) => s.replaceAll("&", "&amp;").replaceAll("<", "&lt;");
    env.FONTCONFIG_FILE = join(root, "fonts.conf");
    env.FONTCONFIG_PATH = join(tmpdir(), "fonts");
    await writeFile(
      env.FONTCONFIG_FILE,
      `<fontconfig><dir>${xml(env.FONTCONFIG_PATH)}</dir><cachedir>${xml(join(root, "font-cache"))}</cachedir></fontconfig>`,
    );
    env.LD_LIBRARY_PATH = [join(tmpdir(), "al2023/lib"), env.LD_LIBRARY_PATH]
      .filter(Boolean)
      .join(":");
  }
  if (process.env.CHROMIUM_LIBRARY_PATH)
    env.LD_LIBRARY_PATH = process.env.CHROMIUM_LIBRARY_PATH;
  browser = await chromium.launch({
    executablePath,
    env,
    // Keep Playwright's security defaults; chrome.args disables web security.
    args: ["--enable-unsafe-swiftshader"],
  });
  console.log(`Chromium ${browser.version()}`);
  for (const colorScheme of ["light", "dark"])
    for (const width of [1280, 390]) {
      const context = await browser.newContext({
        colorScheme,
        viewport: { width, height: 900 },
      });
      const page = await context.newPage();
      await page.clock.install({ time: now });
      const requests = [],
        errors = [];
      page.on("request", (r) => requests.push(r.url()));
      page.on("pageerror", (e) => errors.push(e.message));
      page.on("console", (m) => {
        if (m.type() === "error") errors.push(m.text());
      });
      let release;
      const scripts = new Promise((done) => {
        release = done;
      });
      // Serve real built assets and the snapshot writer's output under built CSP.
      await context.route(`${origin}/**`, async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith(".js") && path !== "/axe.js") await scripts;
        const file =
          path === "/axe.js"
            ? axePath
            : join(root, path === "/stars" ? "stars.html" : path);
        await route.fulfill({
          body: await readFile(file),
          contentType: mime[extname(file)],
          headers: {
            "Content-Security-Policy": policies.findLast((rule) =>
              rule.path.test(path),
            ).policy,
          },
        });
      });
      const tileOrigin = "https://tiles.openfreemap.org";
      await context.route(tileOrigin + "/styles/liberty", (route) =>
        route.fulfill({
          json: {
            version: 8,
            sources: {
              base: {
                type: "vector",
                tiles: [tileOrigin + "/{z}/{x}/{y}.pbf"],
              },
            },
            layers: [
              {
                id: "places",
                type: "circle",
                source: "base",
                "source-layer": "places",
              },
            ],
          },
          headers: { "Access-Control-Allow-Origin": "*" },
        }),
      );
      let servedTiles = 0;
      await context.route(tileOrigin + "/**/*.pbf", async (route) => {
        // MVT v2 layer "places" with a single point at tile coordinate (0, 0).
        await route.fulfill({
          body: Buffer.from(
            "1a130a06706c616365731207180122030900007802",
            "hex",
          ),
          contentType: "application/vnd.mapbox-vector-tile",
          headers: { "Access-Control-Allow-Origin": "*" },
        });
        servedTiles++;
      });
      await page.goto(`${origin}/stars`, { waitUntil: "commit" });
      const heading = await page.locator("h1").elementHandle();
      assert.ok(await page.locator("#stars-summary").isVisible());
      await expect(page.locator("#stars-summary")).toContainText("Site 1");
      assert.equal(await page.locator("#stars-summary li").count(), 6);
      assert.equal(
        await page
          .locator("#stars-summary time")
          .first()
          .getAttribute("datetime"),
        hour(-2),
      );
      const refreshed = page.waitForResponse((r) =>
        r.url().includes("/summary-"),
      );
      release();
      await refreshed;
      await page.waitForFunction(
        () => document.documentElement.dataset.astryxTheme === "neutral",
      );
      assert.ok(
        await heading.evaluate((node) => node === document.querySelector("h1")),
        "hydration replaced the heading",
      );
      assert.ok(
        !requests.some((url) =>
          /stars-map|maplibre|openfreemap|\/sites-|\/historyMap-/.test(url),
        ),
        "heavy data or map loaded before intent",
      );
      await page.addScriptTag({ url: `${origin}/axe.js` });
      const check = async (state) => {
        const result = await page.evaluate(() => window.axe.run(document));
        assert.deepEqual(
          result.violations.map(({ id, nodes }) => ({
            id,
            targets: nodes.map((n) => n.target),
          })),
          [],
          `${colorScheme}/${width}/${state}`,
        );
        assert.ok(result.passes.some(({ id }) => id === "color-contrast"));
        assert.equal(
          await page
            .locator("[style]")
            .evaluateAll(
              (nodes) => nodes.filter((n) => !n.closest(".stars-map")).length,
            ),
          0,
        );
        console.log(
          `${colorScheme}/${width}/${state}: full axe and authored styles passed`,
        );
      };
      const button = (name) => page.getByRole("button", { name, exact: true });
      await check("hydrated summary");
      await button("Load live forecasts").click();
      const firstSite = page.locator(".stars-site-list button").first();
      await expect(firstSite).toHaveText("Site 2");
      await button("Site 1").click();
      assert.match(
        await page
          .getByRole("region", { name: "Upcoming clear dark hours" })
          .innerText(),
        /4%/,
      );
      await check("live card");
      await page
        .getByRole("group", { name: "Night", exact: true })
        .getByRole("button")
        .last()
        .click();
      assert.ok(new URL(page.url()).searchParams.has("night"));
      await expect(
        page.getByText("93 sites. Select a site to see its clear hours.", {
          exact: true,
        }),
      ).toBeVisible();
      await expect(firstSite).toHaveText("Site 1");
      await button("All nights").click();
      await expect(firstSite).toHaveText("Site 2");
      await button("Historical").click();
      await button("February").click();
      await button("Site 1").click();
      await expect(page.locator(".stars-month-chart rect")).toHaveCount(12);
      await expect(page.locator(".stars-metrics dd").first()).toHaveText("2");
      await button("March").click();
      await expect(page.locator(".stars-metrics dd").first()).toHaveText("3");
      assert.equal(new URL(page.url()).search, "?mode=historical&month=3");
      assert.equal(
        requests.filter((url) => /\/historyMap-/.test(url)).length,
        1,
      );
      await check("history card and month filter");
      assert.ok(
        !requests.some((url) => /stars-map|maplibre|openfreemap/.test(url)),
      );
      await button("Close site card").click();
      await button("Load interactive map").click();
      await page.locator("canvas.maplibregl-canvas").waitFor();
      await check("map");
      assert.ok(
        requests.some((url) =>
          /\/assets\/maplibre-gl-worker-csp2-[\w-]+\.js$/.test(url),
        ),
      );
      assert.ok(
        requests.includes("https://tiles.openfreemap.org/styles/liberty"),
      );
      await expect(async () => {
        await page.locator("canvas.maplibregl-canvas").press("Enter");
        await expect(
          page.getByRole("region", { name: "Clear dark hours by month" }),
        ).toBeVisible({ timeout: 100 });
      }).toPass({ timeout: 15_000 });
      await expect.poll(() => servedTiles).toBeGreaterThan(0);
      await expect(page.getByText(/The map could not load/)).toBeHidden();
      await check("keyboard map selection");
      for (let i = 0; i < 3; i++) await button("Next sites").click();
      await button("Site 95").click();
      await check("last historical site");
      assert.deepEqual(errors, []);
      assert.ok(
        requests.every(
          (url) =>
            url.startsWith(origin) ||
            url.startsWith("https://tiles.openfreemap.org/"),
        ),
      );
      assert.ok(!requests.some((url) => url.includes("/api/")));
      // A failed map module still leaves every live/history site accessible.
      await context.route("**/assets/stars-map-*.js", (route) => route.abort());
      await page.reload();
      await page.addScriptTag({ url: `${origin}/axe.js` });
      await button("Load interactive map").click();
      await page.getByText(/Reload the page to retry/).waitFor();
      for (const mode of ["Live", "Historical"]) {
        await button(mode).click();
        await button("Site 1").waitFor();
        for (let i = 0; i < 3; i++) await button("Next sites").click();
        await expect(button("Next sites")).toBeDisabled();
        await button("Site 95").click();
        await check(`${mode} fallback page four`);
      }
      await button("February").click();
      await expect(button("Previous sites")).toBeDisabled();
      await button("Site 1").waitFor();
      await context.close();
    }
} finally {
  await browser?.close();
  await rm(root, { recursive: true, force: true });
}
