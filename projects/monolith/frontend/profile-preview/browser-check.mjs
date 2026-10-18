import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

// Usage: node profile-preview/browser-check.mjs <playwright/index.mjs> <output>
// Start Vite with profile-preview/vite.config.js on port 4173 first.
const { chromium } = await import(pathToFileURL(resolve(process.argv[2])).href);
const output = resolve(process.argv[3]);
await mkdir(output, { recursive: true });
const base = "http://127.0.0.1:4173";
const browser = await chromium.launch({ headless: true });
const evidence = [];
try {
  for (const width of [390, 1440]) {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({
        viewport: { width, height: 1000 },
        colorScheme: scheme,
        reducedMotion: "reduce",
      });
      // The actual public layout includes Google Fonts; this offline fixture
      // uses the repository's vendored Schibsted font instead.
      await context.route(
        /https:\/\/fonts\.(googleapis|gstatic)\.com\//,
        (route) => route.fulfill({ contentType: "text/css", body: "" }),
      );
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.clock.setFixedTime(new Date("2026-10-18T12:00:00Z"));
      const noOverflow = async () =>
        assert.equal(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth + 1,
          ),
          true,
          `${page.url()}: horizontal overflow`,
        );
      for (const [path, name] of [
        ["/", "home"],
        ["/cv", "cv"],
      ]) {
        await page.goto(`${base}${path}`, { waitUntil: "networkidle" });
        await page.locator(".profile-sheet h1").waitFor();
        await page.evaluate(() => document.fonts.ready);
        assert.equal(
          await page.evaluate(() =>
            document.fonts.check('16px "Schibsted Grotesk"'),
          ),
          true,
        );
        assert.equal(await page.locator(".chrome").count(), 1);
        assert.equal(await page.locator("button.scheme").count(), 1);
        assert.equal(await page.locator(".md-nav").count(), 0);
        assert.equal(await page.locator("h1").count(), 1);
        await noOverflow();
        const painted = await page.locator("main").evaluate((element) => ({
          background: getComputedStyle(element).backgroundColor,
          ink: getComputedStyle(element).color,
          font: getComputedStyle(element).fontFamily,
        }));
        assert.equal(
          painted.background,
          scheme === "light" ? "rgb(255, 255, 255)" : "rgb(24, 26, 32)",
        );
        const prefix = `${name}-${scheme}-${width}`;
        await page.screenshot({
          path: `${output}/${prefix}.png`,
          fullPage: true,
        });
        await page.screenshot({ path: `${output}/${prefix}-opening.png` });
        evidence.push({
          path,
          scheme,
          width,
          height: 1000,
          screenshots: [`${prefix}.png`, `${prefix}-opening.png`],
          painted,
        });
        const schemeButton = page.locator("button.scheme");
        await schemeButton.focus();
        assert.notEqual(
          await schemeButton.evaluate(
            (element) => getComputedStyle(element).outlineStyle,
          ),
          "none",
        );
        await page.keyboard.press("Enter");
        assert.equal(
          await page.evaluate(() => localStorage.getItem("td-theme")),
          scheme === "light" ? "dark" : "light",
        );
        await page.keyboard.press("Enter");
        await page.reload({ waitUntil: "networkidle" });
        assert.equal(
          await page.evaluate(() => document.documentElement.dataset.theme),
          scheme,
        );
      }
      await page.goto(`${base}/`, { waitUntil: "networkidle" });
      await page.locator('a[href="#homelab"]').last().click();
      assert.equal(new URL(page.url()).hash, "#homelab");
      assert.equal(await page.locator("#homelab").isVisible(), true);
      assert.equal(
        await page
          .getByRole("link", { name: "Slop Factory", exact: true })
          .getAttribute("href"),
        "/slop/factory",
      );
      await page.evaluate(() => {
        window.fixtureNavigationSentinel = true;
      });
      await page
        .getByRole("navigation", { name: "Continue reading" })
        .getByRole("link", { name: "CV", exact: true })
        .click();
      await page
        .getByRole("heading", { name: "Joe McGinley", exact: true })
        .waitFor();
      await page
        .getByRole("navigation", { name: "Continue reading" })
        .getByRole("link", { name: "Engineering", exact: true })
        .click();
      await page.locator("main.eng-page").waitFor();
      assert.equal(await page.locator("button.scheme").count(), 1);
      await noOverflow();
      await page
        .getByRole("link", { name: "Legacy Ember fixture", exact: true })
        .click();
      await page.locator(".ember-site").waitFor();
      assert.equal(
        await page.locator(".td, button.scheme, .md-nav").count(),
        0,
      );
      await noOverflow();
      const legacyPaint = await page.locator(".ember-site").evaluate((el) => ({
        background: getComputedStyle(el).backgroundColor,
        font: getComputedStyle(el).fontFamily,
      }));
      await page
        .getByRole("link", { name: "Legacy Nav fixture", exact: true })
        .click();
      assert.equal(await page.locator(".md-nav").count(), 1);
      assert.equal(await page.locator("button.scheme").count(), 0);
      await page.getByRole("button", { name: /APPS/ }).click();
      await page.keyboard.press("Escape");
      await page
        .getByRole("link", { name: "Home fixture", exact: true })
        .click();
      assert.equal(await page.locator("button.scheme").count(), 1);
      assert.equal(await page.locator(".md-nav").count(), 0);
      assert.equal(
        await page.evaluate(() => window.fixtureNavigationSentinel),
        true,
        "navigation replaced the document",
      );
      assert.equal(
        await page.evaluate(() => document.documentElement.dataset.theme),
        scheme,
      );
      await page.goto(`${base}/ember`, { waitUntil: "networkidle" });
      assert.deepEqual(
        await page.locator(".ember-site").evaluate((el) => ({
          background: getComputedStyle(el).backgroundColor,
          font: getComputedStyle(el).fontFamily,
        })),
        legacyPaint,
      );
      await page.goto(`${base}/?empty&banner`, { waitUntil: "networkidle" });
      assert.equal(
        await page.getByText("Stats unavailable.", { exact: true }).count(),
        1,
      );
      assert.equal(
        await page.locator(".maintenance-banner").textContent(),
        "Synthetic maintenance notice",
      );
      await noOverflow();
      await page.screenshot({
        path: `${output}/empty-banner-${scheme}-${width}.png`,
        fullPage: true,
      });
      evidence.push({
        scheme,
        width,
        navigation:
          "home / CV / engineering / legacy Ember / legacy Nav / home",
        themePersistence: "pass",
        keyboardFocus: "pass",
        emptyBanner: "pass",
        legacyFreshAndNavigationPaint: legacyPaint,
        errors,
      });
      assert.deepEqual(errors, []);
      await context.close();
    }
  }
} finally {
  await browser.close();
  await writeFile(
    `${output}/checks.json`,
    JSON.stringify(evidence, null, 2) + "\n",
  );
}
console.log(`PASS: ${evidence.length} capture/navigation records in ${output}`);
