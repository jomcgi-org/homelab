// :leaderboard_browser_test runs in PR CI with an npm-packaged Chromium.
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { preview } from "vite";
import axe from "axe-core";
import { chromium } from "playwright";
import "./leaderboard-built.mjs";

const dist = resolve(process.argv[2]);
const csp = readFileSync(join(dist, "_headers"), "utf8").match(
  /Content-Security-Policy: (.*)/,
)[1];
const server = await preview({
  configFile: false,
  appType: "mpa",
  build: { outDir: dist },
  preview: {
    host: "127.0.0.1",
    port: 0,
    headers: { "Content-Security-Policy": csp },
  },
});
let browser;
const scratch = mkdtempSync(join(tmpdir(), "leaderboard-browser-"));
try {
  let executablePath = process.env.SITE_CHROMIUM_EXECUTABLE;
  if (!executablePath) {
    process.env.TMPDIR = scratch;
    const { default: chrome, inflate } = await import("@sparticuz/chromium");
    const require = createRequire(import.meta.url);
    const bin = join(dirname(require.resolve("@sparticuz/chromium")), "../bin");
    await inflate(join(bin, "al2023.tar.br"));
    executablePath = await chrome.executablePath();
    const xml = (path) => path.replaceAll("&", "&amp;").replaceAll("<", "&lt;");
    process.env.FONTCONFIG_FILE = join(scratch, "fonts.conf");
    writeFileSync(
      process.env.FONTCONFIG_FILE,
      `<fontconfig><dir>${xml(join(scratch, "fonts"))}</dir><cachedir>${xml(join(scratch, "cache"))}</cachedir></fontconfig>`,
    );
    process.env.LD_LIBRARY_PATH = [
      join(scratch, "al2023/lib"),
      process.env.LD_LIBRARY_PATH,
    ]
      .filter(Boolean)
      .join(":");
  }
  // Playwright defaults preserve web security and the production CSP.
  browser = await chromium.launch({
    executablePath,
  });
  console.log(`Chromium ${browser.version()}`);
  for (const width of [390, 1280])
    for (const colorScheme of ["light", "dark"]) {
      const context = await browser.newContext({
        viewport: { width, height: 900 },
        colorScheme,
      });
      const page = await context.newPage();
      const beacon = "https://static.cloudflareinsights.com/beacon.min.js";
      await page.route(beacon, (route) =>
        route.fulfill({ contentType: "text/javascript", body: "" }),
      );
      const bars = page.locator(".bars");
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      let release,
        requests = 0;
      const gate = new Promise((resolve) => {
        release = resolve;
      });
      await page.route("**/leaderboard-interactive-*.js", async (route) => {
        requests++;
        await gate;
        await route.continue();
      });
      await page.route("**/__axe.js", (route) =>
        route.fulfill({ contentType: "text/javascript", body: axe.source }),
      );
      await page.goto(`${server.resolvedUrls.local[0]}llm-leaderboard`, {
        waitUntil: "networkidle",
      });
      assert.equal(requests, 0, "report JS must wait for intent");
      assert.equal(await bars.count(), 4);
      await page.evaluate(() => {
        window.reportNode = document.querySelector(".lb-page");
      });
      const download = page.waitForRequest("**/leaderboard-interactive-*.js");
      await page
        .getByRole("button", { name: "Clear all", exact: true })
        .click();
      await download;
      assert.equal(await bars.count(), 4, "SSR stays readable during download");
      release();
      await page
        .getByText("No models selected. Pick a preset or search above.")
        .waitFor();
      assert.equal(requests, 1);
      assert(
        await page.evaluate(() => window.reportNode.isConnected),
        "hydration must reuse the report node",
      );
      await page.addScriptTag({ url: "/__axe.js" });
      for (const name of ["All", "Self-hosted", "Clear all"]) {
        await page.getByRole("button", { name, exact: true }).click();
        if (name !== "Clear all")
          await page
            .getByRole("group", { name: "X axis metric" })
            .getByRole("button", { name: "Tokens per task", exact: true })
            .click();
        assert.equal(await page.locator("[style]:not([style=''])").count(), 0);
        const violations = await page.evaluate(
          async () => (await window.axe.run()).violations,
        );
        assert.deepEqual(violations, [], `${width}/${colorScheme}/${name}`);
      }
      assert.deepEqual(errors, [], "no browser or hydration errors");
      console.log(`ok   ${width}/${colorScheme}: deferred hydration and axe`);
      await context.close();
    }
} finally {
  await browser?.close();
  await server.close();
  rmSync(scratch, { recursive: true, force: true });
}
