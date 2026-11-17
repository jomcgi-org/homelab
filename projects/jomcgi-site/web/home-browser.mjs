// Exercise the production bundle, CSS and portrait in Chromium (HOME-206:8/9).
// Supply PLAYWRIGHT_MODULE (tested with 1.63.0); CHROMIUM_PATH is optional.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { preview } from "vite";
import axe from "axe-core";
import { latestPosts, posts } from "./site-inputs.mjs";

const { chromium } = await import(
  process.env.PLAYWRIGHT_MODULE || "playwright"
);
const root = fileURLToPath(new URL("./", import.meta.url));
const read = (file) => readFileSync(`${root}/dist/${file}`, "utf8");
const manifest = JSON.parse(read(".vite/manifest.json"));
const entry = manifest["index.html"].file;
const engine = manifest["src/home-portrait.js"].file;
const server = await preview({
  configFile: false,
  root,
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
    executablePath: process.env.CHROMIUM_PATH,
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
          requests.every((url) => url.startsWith(`${origin}/`)),
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
}
