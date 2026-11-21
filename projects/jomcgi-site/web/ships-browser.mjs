// Compiled-page acceptance: pnpm build && pnpm test:browser.
import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve } from "node:path";
import chrome, { inflate } from "@sparticuz/chromium";
import { chromium } from "playwright";
import { expect } from "playwright/test";
const root = resolve(process.argv[2]);
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
const origin = "https://jomcgi.dev";
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
    env.FONTCONFIG_FILE = join(tmpdir(), "ships-fonts.conf");
    env.FONTCONFIG_PATH = join(tmpdir(), "fonts");
    await writeFile(
      env.FONTCONFIG_FILE,
      `<fontconfig><dir>${xml(env.FONTCONFIG_PATH)}</dir><cachedir>${xml(join(tmpdir(), "ships-font-cache"))}</cachedir></fontconfig>`,
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

  const context = await browser.newContext();
  await context.addCookies([
    {
      name: "fixture",
      value: "private",
      url: "https://factory-data.jomcgi.dev",
      sameSite: "None",
      secure: true,
    },
  ]);
  const page = await context.newPage();
  page.on("console", (message) => {
    if (message.type() === "error") console.error(message.text());
  });
  await page.clock.install();
  const requests = [];
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const digest = "a".repeat(64);
  const track = "tracks/111-" + digest + ".json";
  const heat = "heat-" + digest + ".json";
  let generation = 1;
  let missing = false;
  let retired = false;
  const timestamp = "2020-01-01T00:00:00+00:00";
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.href === "https://static.cloudflareinsights.com/beacon.min.js")
      return route.fulfill({ contentType: "text/javascript", body: "" });
    if (url.origin === origin) {
      const file = join(
        root,
        url.pathname + (url.pathname === "/app/ships" ? ".html" : ""),
      );
      await route.fulfill({
        body: await readFile(file),
        contentType: mime[extname(file)],
        headers: {
          "Content-Security-Policy": policies.findLast((p) =>
            p.path.test(url.pathname),
          ).policy,
        },
      });
      return;
    }
    const headers = { "Access-Control-Allow-Origin": origin };
    if (url.href === "https://tiles.openfreemap.org/styles/liberty") {
      await route.fulfill({
        headers,
        json: {
          version: 8,
          sources: {},
          layers: [{ id: "background", type: "background" }],
        },
      });
      return;
    }
    assert.equal(url.origin, "https://factory-data.jomcgi.dev");
    assert.ok(url.pathname.startsWith("/ships/"));
    const requestHeaders = await route.request().allHeaders();
    assert.equal(requestHeaders.authorization, undefined);
    assert.equal(requestHeaders.cookie, undefined);
    const key = url.pathname.slice("/ships/".length);
    requests.push(key);
    const snapshot = "snapshot-" + String(generation).repeat(64) + ".json";
    const products = {
      "latest.json": { snapshot, heat },
      [snapshot]: {
        generated_at: timestamp,
        vessels: [
          {
            mmsi: "111",
            name: "Test vessel " + generation,
            lat: 48,
            lon: -123,
            recorded_at: timestamp,
            track: missing ? null : track,
          },
        ],
      },
      [heat]: {
        computed_at: timestamp,
        cells: [],
        stops: [1, 2, 3, 4, 5, 6],
        step_lat: 0.01,
        step_lon: 0.01,
      },
      [track]: { count: 0, track: [] },
    };
    if (retired && key === track) {
      await route.fulfill({ headers, status: 404 });
      return;
    }
    assert.ok(key in products);
    await route.fulfill({ headers, json: products[key] });
  });
  async function swatchColours(selector) {
    return page
      .locator(selector)
      .evaluateAll(
        (nodes) =>
          new Set(nodes.map((node) => getComputedStyle(node).backgroundColor))
            .size,
      );
  }
  await page.goto(origin + "/app/ships?mmsi=111");
  await expect(page.getByText("0 track points")).toBeVisible();
  await expect(page.locator("time")).toHaveText(timestamp);
  assert.equal(await swatchColours(".vessel-sw"), 6);
  assert.ok(!requests.includes(heat));
  generation++;
  missing = true;
  await page.clock.fastForward(120001);
  await expect(
    page.getByRole("heading", { name: "Test vessel 2" }),
  ).toBeVisible();
  await expect(page.getByText("Track unavailable")).toBeVisible();
  missing = false;
  retired = true;
  await page.goto(origin + "/app/ships?mmsi=111");
  await expect(page.getByText("Track unavailable")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Test vessel 2" }),
  ).toBeVisible();
  requests.length = 0;
  await page.goto(origin + "/app/ships?mode=heat");
  await expect.poll(() => requests.includes(heat)).toBe(true);
  await expect(page.getByText("Heat computed")).toBeVisible();
  assert.equal(await swatchColours(".heat-sw"), 6);
  assert.ok(!requests.includes(track));
  assert.deepEqual(errors, []);
  // Run from an environment with mirror egress before moving reader traffic.
  if (process.env.SHIPS_LIVE_CHECK === "1") {
    await context.route("https://factory-data.jomcgi.dev/ships/**", (route) =>
      route.continue(),
    );
    await page.goto(origin + "/app/ships?mode=heat");
    await expect(page.locator("time").first()).toBeVisible();
    await expect(page.getByText("Heat computed")).toBeVisible();
  }
} finally {
  await browser?.close();
}
