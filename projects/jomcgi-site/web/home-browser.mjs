// Exercise the production bundle, CSS and portrait in Chromium (HOME-206:8/9).
// Usage: node home-browser.mjs <dist>; CI target :home_browser_test.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { preview } from "vite";
import axe from "axe-core";
import { chromium } from "playwright";
import { latestPosts, posts } from "./site-inputs.mjs";

const dist = resolve(process.argv[2] ?? "dist");
const read = (file) => readFileSync(join(dist, file), "utf8");
const manifest = JSON.parse(read(".vite/manifest.json"));
const entry = manifest["index.html"].file;
const engine = manifest["src/home-portrait.js"].file;
const server = await preview({
  configFile: false,
  build: { outDir: dist },
  preview: {
    host: "127.0.0.1",
    port: 0,
    headers: {
      "Content-Security-Policy": read("_headers").match(
        /Content-Security-Policy: (.+)/,
      )[1],
    },
  },
});
const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
let browser;
const scratch = mkdtempSync(join(tmpdir(), "home-browser-"));
// Chromium from @sparticuz/chromium, with its libraries and fonts unpacked into
// the scratch directory (as in leaderboard-browser.mjs).
async function chromiumPath() {
  if (process.env.SITE_CHROMIUM_EXECUTABLE)
    return process.env.SITE_CHROMIUM_EXECUTABLE;
  process.env.TMPDIR = scratch;
  const { default: chrome, inflate } = await import("@sparticuz/chromium");
  const pkg = createRequire(import.meta.url).resolve("@sparticuz/chromium");
  await inflate(join(dirname(pkg), "../bin/al2023.tar.br"));
  const executablePath = await chrome.executablePath();
  const xml = (path) => path.replaceAll("&", "&amp;").replaceAll("<", "&lt;");
  const conf = `<fontconfig><dir>${xml(join(scratch, "fonts"))}</dir><cachedir>${xml(join(scratch, "cache"))}</cachedir></fontconfig>`;
  process.env.FONTCONFIG_FILE = join(scratch, "fonts.conf");
  writeFileSync(process.env.FONTCONFIG_FILE, conf);
  const libs = [join(scratch, "al2023/lib"), process.env.LD_LIBRARY_PATH];
  process.env.LD_LIBRARY_PATH = libs.filter(Boolean).join(":");
  return executablePath;
}
async function accessible(page, label) {
  const violations = await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise(requestAnimationFrame);
    await Promise.all(
      document
        .getAnimations()
        .filter(
          (animation) => animation.effect.getTiming().iterations !== Infinity,
        )
        .map((animation) => animation.finished.catch(() => {})),
    );
    return (
      await window.axe.run(document, {
        rules: { "label-content-name-mismatch": { enabled: true } },
      })
    ).violations.map(({ id, nodes }) => ({
      id,
      targets: nodes.map((node) => node.target),
    }));
  });
  assert.deepEqual(violations, [], label); // Includes real CSS color-contrast.
  assert.equal(await page.locator("[style], style").count(), 0, label);
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
    label,
  );
}
try {
  browser = await chromium.launch({
    executablePath: await chromiumPath(),
    args: ["--no-sandbox", "--disable-dev-shm-usage"],
  });
  for (const [width, height, intent] of [
    [1280, 900, "keyboard"],
    [390, 844, "move"],
    [844, 390, "press"],
  ]) {
    for (const colorScheme of ["light", "dark"]) {
      for (const reducedMotion of ["no-preference", "reduce"]) {
        const label = `${width}x${height}/${colorScheme}/${reducedMotion}`;
        const context = await browser.newContext({
          viewport: { width, height },
          colorScheme,
          reducedMotion,
        });
        const page = await context.newPage();
        const beacon = "https://static.cloudflareinsights.com/beacon.min.js";
        await page.route(beacon, (route) =>
          route.fulfill({ contentType: "text/javascript", body: "" }),
        );
        const errors = [],
          requests = [];
        page.on("pageerror", (error) => errors.push(error.message));
        page.on("console", (message) => {
          if (message.type() === "error") errors.push(message.text());
        });
        page.on("request", (request) => requests.push(request.url()));
        page.on("response", (response) => {
          if (response.status() >= 400)
            errors.push(`${response.status()} ${response.url()}`);
        });
        // Hold the real entry script while inspecting the server-rendered DOM.
        // Releasing it must hydrate those same nodes, without mounting afresh.
        let release;
        const held = new Promise((resolve) => {
          release = resolve;
        });
        await page.route(`${origin}/${entry}`, async (route) => {
          await held;
          await route.continue();
        });
        await page.goto(origin, { waitUntil: "commit" });
        const still = await page.waitForSelector(".portrait-still");
        await page.waitForFunction(
          () => document.querySelector(".portrait-still").naturalWidth > 0,
        );
        assert.equal(await page.locator(".meta b").textContent(), "--:--:--");
        release();
        await page.waitForLoadState("networkidle");
        await page.waitForFunction(
          () => document.querySelector(".meta b").textContent !== "--:--:--",
        );
        assert.ok(
          await still.evaluate(
            (node) => node === document.querySelector(".portrait-still"),
          ),
          "hydrate keeps server DOM",
        );
        const clock = await page.locator(".meta b").textContent();
        await page.waitForFunction(
          (before) => document.querySelector(".meta b").textContent !== before,
          clock,
        );
        const loads = () =>
          requests.filter((url) => url === `${origin}/${engine}`).length;
        assert.equal(loads(), 0, "no portrait JS before intent");
        await page.evaluate(axe.source);
        await accessible(page, `${label}/initial`);
        if (intent === "keyboard") await page.keyboard.press("Tab");
        if (intent === "move") await page.mouse.move(10, 10);
        if (intent === "press") {
          await page.mouse.down();
          await page.mouse.up();
        }
        if (reducedMotion === "no-preference")
          await page.waitForSelector(".print.ready");
        await page.getByRole("link", { name: "ships", exact: true }).focus();
        assert.match(
          await page.locator(".glass .small").textContent(),
          width > 760 ? /Every ship off Scotland/ : /Semgrep/,
        );
        await accessible(page, `${label}/key-focus`);
        if (width > 760) {
          await page.locator(".writing a").first().focus();
          assert.equal(
            await page.locator(".glass .small").textContent(),
            latestPosts(posts)[0].summary,
          );
          await accessible(page, `${label}/post-focus`);
        }
        await page.getByRole("button", { name: /Switch to/ }).click();
        const next = colorScheme === "light" ? "dark" : "light";
        assert.equal(
          await page.evaluate(() => localStorage.getItem("td-theme")),
          next,
        );
        assert.equal(
          await page.locator("html").getAttribute("data-home-scheme"),
          next,
        );
        await accessible(page, `${label}/toggled`);
        assert.equal(
          loads(),
          reducedMotion === "reduce" ? 0 : 1,
          "one lazy load, none for reduced motion",
        );
        await page.reload({ waitUntil: "networkidle" });
        await page.waitForFunction(
          (saved) => document.documentElement.dataset.homeScheme === saved,
          next,
        );
        assert.deepEqual(errors, [], label);
        assert.ok(
          requests.every(
            (url) => url === beacon || url.startsWith(`${origin}/`),
          ),
          "no hub/API requests",
        );
        console.log(`ok ${label}`);
        await context.close();
      }
    }
  }
  console.log(`Chromium ${browser.version()}: HOME-206 browser checks passed`);
} finally {
  await browser?.close();
  await new Promise((resolve) => server.httpServer.close(resolve));
  rmSync(scratch, { recursive: true, force: true });
}
