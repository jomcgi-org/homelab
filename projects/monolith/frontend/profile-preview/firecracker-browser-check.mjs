import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { cold, restores } from "../src/lib/public/fcstory/data/trace.js";

// Same production-composed fixture as browser-check.mjs. The separate
// adapter-node server supplies real server-rendered HTML for the no-JS check.
const { chromium } = await import(pathToFileURL(resolve(process.argv[2])).href);
const output = resolve(process.argv[3]);
const base = process.env.FIRECRACKER_PREVIEW_URL || "http://127.0.0.1:4173";
const ssrBase = process.env.FIRECRACKER_SSR_URL || "http://127.0.0.1:4174";
const sourceSha = execFileSync("git", ["rev-parse", "HEAD"], {
  encoding: "utf8",
}).trim();
const trackedTreeClean = !execFileSync(
  "git",
  ["status", "--porcelain", "--untracked-files=no"],
  { encoding: "utf8" },
).trim();
if (process.env.FACTORY_PREVIEW_SHA) {
  assert.equal(sourceSha, process.env.FACTORY_PREVIEW_SHA);
  assert.equal(trackedTreeClean, true, "Capture must match the PR head");
}
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE_PATH
    ? { executablePath: process.env.CHROMIUM_EXECUTABLE_PATH }
    : {}),
});
const evidence = [];
const errors = [];
const network = [];
let passed = false;
try {
  for (const width of [390, 1440]) {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({
        viewport: { width, height: 1000 },
        colorScheme: scheme,
        reducedMotion: "no-preference",
      });
      await context.route(
        /https:\/\/fonts\.(googleapis|gstatic)\.com\//,
        (route) => route.fulfill({ contentType: "text/css", body: "" }),
      );
      const page = await context.newPage();
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("request", (request) => {
        if (["fetch", "xhr"].includes(request.resourceType()))
          network.push(request.url());
      });
      const record = { sourceSha, width, height: 1000, scheme, states: [] };
      evidence.push(record);
      const capture = async (state, fullPage = false) => {
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - innerWidth,
        );
        const screenshot = `firecracker-${scheme}-${width}-${state}.png`;
        await page.screenshot({ path: `${output}/${screenshot}`, fullPage });
        record.states.push({ state, screenshot, overflow });
        assert.equal(
          overflow,
          0,
          `${scheme}/${width}/${state}: horizontal page overflow`,
        );
      };
      await page.goto(`${base}/ember/firecracker?source=cv`, {
        waitUntil: "networkidle",
      });
      await page.evaluate(() => document.fonts.ready);
      assert.equal(await page.locator("main.td.sheet").count(), 1);
      assert.equal(await page.locator(".chrome").count(), 1);
      assert.equal(await page.locator("button.scheme").count(), 1);
      assert.equal(await page.locator(".md-nav").count(), 0);
      assert.equal(await page.locator(".edition").count(), 6);
      assert.equal(
        await page.locator(".recorded-runs li").count(),
        restores.length,
      );
      assert.equal(
        await page.title(),
        "Boot once, restore forever · Firecracker microVMs · jomcgi.dev",
      );
      record.paint = await page.locator("main").evaluate((element) => ({
        background: getComputedStyle(element).backgroundColor,
        color: getComputedStyle(element).color,
      }));
      assert.equal(
        record.paint.background,
        scheme === "light" ? "rgb(255, 255, 255)" : "rgb(24, 26, 32)",
      );
      await capture("story", true);
      await page.locator("#restore").scrollIntoViewIfNeeded();
      await capture("restore");
      await page
        .locator('nav[aria-label="On this page"] a[href="#replay"]')
        .click();
      assert.equal(new URL(page.url()).hash, "#replay");
      assert.equal(new URL(page.url()).searchParams.get("source"), "cv");
      const replay = page.getByRole("button", { name: "Restore again" });
      // Enter keyboard modality after the pointer-driven index navigation.
      await page.keyboard.press("Tab");
      await replay.focus();
      assert.notEqual(
        await replay.evaluate(
          (element) => getComputedStyle(element).outlineStyle,
        ),
        "none",
      );
      await page.keyboard.press("Enter");
      await page.waitForFunction(
        () => document.querySelector(".counter b").textContent === "1",
      );
      assert.equal(
        await page.locator(".final").textContent(),
        `${restores[0].total.toFixed(1)} ms`,
      );
      await capture("replay");
      const slow = page.getByRole("checkbox", { name: "slow motion" });
      await slow.focus();
      await page.keyboard.press("Space");
      await replay.click();
      assert.equal(await replay.isDisabled(), true);
      assert.equal(await slow.isDisabled(), true);
      await capture("slow-busy");
      await page.waitForFunction(
        () => document.querySelector(".counter b").textContent === "2",
      );
      assert.match(
        await page.locator(".replay-readout").textContent(),
        /40× slower/,
      );
      await slow.uncheck();
      for (let i = 2; i <= restores.length; i++) {
        await replay.click();
        await page.waitForFunction(
          (count) =>
            document.querySelector(".counter b").textContent === String(count),
          i + 1,
        );
        const run = restores[i % restores.length];
        assert.equal(
          await page.locator(".final").textContent(),
          `${run.total.toFixed(1)} ms`,
        );
        const rows = await page.locator(".phase-table tr").allTextContents();
        assert.equal(rows.length, run.phases.length);
        run.phases.forEach((phase, index) => {
          assert.ok(rows[index].includes(phase.name));
          assert.ok(rows[index].includes(`${phase.ms.toFixed(1)} ms`));
        });
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth - innerWidth,
          ),
          0,
        );
      }
      record.recordedRunsAndWrap = true;
      await page.emulateMedia({ reducedMotion: "reduce" });
      await replay.click();
      assert.equal(await replay.isDisabled(), false);
      await capture("reduced-motion", true);
      const toggle = page.locator("button.scheme");
      await page.keyboard.press("Tab");
      await toggle.focus();
      assert.notEqual(
        await toggle.evaluate(
          (element) => getComputedStyle(element).outlineStyle,
        ),
        "none",
      );
      await page.keyboard.press("Enter");
      const opposite = scheme === "light" ? "dark" : "light";
      assert.equal(
        await page.evaluate(() => localStorage.getItem("td-theme")),
        opposite,
      );
      await page.reload({ waitUntil: "networkidle" });
      assert.equal(
        await page.evaluate(() => document.documentElement.dataset.theme),
        opposite,
      );
      await page.locator("button.scheme").click();
      record.keyboardAndPersistence = true;

      // Component navigation, including leaving during a slow animation.
      await page.emulateMedia({ reducedMotion: "no-preference" });
      await slow.check();
      await replay.click();
      await page.locator('.fixture-links a[href="/engineering"]').click();
      if (width === 1440) {
        await page.locator('.eng-page nav[aria-label="Site"] summary').click();
        await page.locator('.eng-page a[href="/ember/firecracker"]').click();
      } else {
        // The site's desktop spine intentionally hides on a phone. Home
        // supplies a visible production link at both widths.
        await page.locator('.fixture-links a[href="/"]').click();
        await page
          .locator('.profile-sheet a[href="/ember/firecracker"]:visible')
          .click();
      }
      assert.equal(await page.locator(".counter b").textContent(), "0");
      await page.locator('.fixture-links a[href="/ember"]').click();
      assert.equal(await page.locator(".fcstory, .chrome").count(), 0);
      await page
        .locator(".ember-site summary")
        .filter({ hasText: "the moving parts" })
        .click();
      await page.locator('.ember-site a[href="/ember/firecracker"]').click();
      await page.locator('.fixture-links a[href="/legacy-fixture"]').click();
      assert.equal(await page.locator(".md-nav").count(), 1);
      await page.locator('.fixture-links a[href="/ember/firecracker"]').click();
      assert.equal(await page.locator(".md-nav").count(), 0);
      assert.equal(await page.locator(".chrome").count(), 1);
      await capture("navigation");
      await page.setViewportSize({
        width: width === 390 ? 1440 : 390,
        height: 740,
      });
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth - innerWidth,
        ),
        0,
      );
      record.resizeOverflow = 0;
      await context.close();

      // Actual production SSR, not the client-only fixture with scripting off.
      const nojs = await browser.newContext({
        viewport: { width, height: 1000 },
        colorScheme: scheme,
        javaScriptEnabled: false,
      });
      await nojs.route(
        /https:\/\/fonts\.(googleapis|gstatic)\.com\//,
        (route) => route.fulfill({ contentType: "text/css", body: "" }),
      );
      const staticPage = await nojs.newPage();
      const response = await staticPage.goto(
        `${ssrBase}/ember/firecracker?source=cv#replay`,
        { waitUntil: "networkidle" },
      );
      assert.equal(response.status(), 200);
      assert.equal(
        await staticPage.locator("main.td.sheet .edition").count(),
        6,
      );
      assert.equal(
        await staticPage.locator(".recorded-runs li").count(),
        restores.length,
      );
      assert.equal(
        await staticPage.locator(".replay-controls").isVisible(),
        false,
      );
      assert.ok(
        (await staticPage.locator("main").textContent()).includes(
          cold.total.toLocaleString(),
        ),
      );
      const overflow = await staticPage.evaluate(
        () => document.documentElement.scrollWidth - innerWidth,
      );
      assert.equal(overflow, 0);
      const screenshot = `firecracker-${scheme}-${width}-no-js.png`;
      await staticPage.screenshot({
        path: `${output}/${screenshot}`,
        fullPage: true,
      });
      record.noJavaScript = {
        productionSSR: true,
        status: response.status(),
        overflow,
        screenshot,
      };
      await nojs.close();
    }
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(network, [], "Story/fixture must not fetch a backend");
  passed = true;
} finally {
  await writeFile(
    `${output}/checks.json`,
    JSON.stringify(
      { sourceSha, trackedTreeClean, passed, evidence, errors, network },
      null,
      2,
    ),
  );
  await browser.close();
}
console.log(
  `Firecracker browser checks passed at ${sourceSha}; ${evidence.length} width/scheme combinations, 0px overflow.`,
);
