import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
import { preview } from "vite";

// Usage: node profile-preview/browser-check.mjs <playwright/index.mjs> <output>
// Build profile-preview/vite.config.js first. Serve the built artifact here.
const sourceHead = execFileSync("git", ["rev-parse", "HEAD"], {
  encoding: "utf8",
}).trim();
if (process.env.PROFILE_PREVIEW_SHA)
  assert.equal(sourceHead, process.env.PROFILE_PREVIEW_SHA);
const { chromium } = await import(pathToFileURL(resolve(process.argv[2])).href);
const output = resolve(process.argv[3]);
await mkdir(output, { recursive: true });
const base = "http://127.0.0.1:4173";
const server = await preview({
  configFile: "profile-preview/vite.config.js",
  preview: { host: "127.0.0.1", port: 4173, strictPort: true },
});
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
      const noOverflow = async () => {
        const measurement = await page.evaluate(() => ({
          viewportWidth: innerWidth,
          documentWidth: document.documentElement.scrollWidth,
          bodyWidth: document.body.scrollWidth,
          overflow: Math.max(
            0,
            document.documentElement.scrollWidth - innerWidth,
            document.body.scrollWidth - innerWidth,
          ),
        }));
        assert.equal(
          measurement.overflow,
          0,
          `${page.url()}: horizontal overflow`,
        );
        return measurement;
      };
      const textContrast = async () => {
        const samples = await page
          .locator(
            ".ember-sheet h1, .ember-sheet .lede, .ember-sheet .ctag, .ember-sheet .cmore p, .ember-sheet .ed-lead p, .ember-sheet .foot, .ember-sheet .trail .crumb, .ember-sheet .index-list a",
          )
          .evaluateAll((elements) => {
            const luminance = (color) => {
              const channels = color
                .match(/[\d.]+/g)
                .slice(0, 3)
                .map(Number)
                .map((n) => {
                  n /= 255;
                  return n <= 0.04045
                    ? n / 12.92
                    : ((n + 0.055) / 1.055) ** 2.4;
                });
              return (
                channels[0] * 0.2126 +
                channels[1] * 0.7152 +
                channels[2] * 0.0722
              );
            };
            return elements
              .filter((el) => el.checkVisibility())
              .map((el) => {
                let parent = el;
                while (
                  parent &&
                  getComputedStyle(parent).backgroundColor ===
                    "rgba(0, 0, 0, 0)"
                )
                  parent = parent.parentElement;
                const foreground = getComputedStyle(el).color;
                const background = getComputedStyle(parent).backgroundColor;
                const a = luminance(foreground),
                  b = luminance(background);
                return {
                  text: el.textContent.trim().slice(0, 80),
                  foreground,
                  background,
                  ratio: (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05),
                };
              });
          });
        for (const sample of samples)
          assert.ok(
            sample.ratio >= 4.5,
            `${page.url()}: contrast ${JSON.stringify(sample)}`,
          );
        return samples;
      };
      for (const [path, name] of [
        ["/", "home"],
        ["/cv", "cv"],
        ["/ember", "ember"],
        ["/ember/bazel", "bazel-fallback"],
        ["/ember/postgres", "postgres-fallback"],
      ]) {
        await page.goto(`${base}${path}`, { waitUntil: "networkidle" });
        await page.locator("main.td.sheet").waitFor();
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
        assert.equal(
          await page.locator("h1").count(),
          name.endsWith("-fallback") ? 0 : 1,
        );
        const overflow = await noOverflow();
        if (path.startsWith("/ember")) {
          assert.equal(
            await page
              .getByRole("navigation", { name: "You are here" })
              .filter({ visible: true })
              .count(),
            1,
          );
          assert.equal(await page.locator(".topbar, .ember-site").count(), 0);
          if (name.endsWith("-fallback")) {
            assert.equal(
              await page.locator(".ed-lead p").innerText(),
              "Continue to the recorded replay.",
            );
            assert.equal(
              await page
                .getByRole("link", { name: "the recorded replay", exact: true })
                .getAttribute("href"),
              "/ember/firecracker#replay",
            );
          }
        }
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
          sourceHead,
          surface: name,
          kind: name.endsWith("-fallback")
            ? "component fixture; loader not executed"
            : "production component in fixture layouts",
          path,
          scheme,
          width,
          height: 1000,
          screenshots: [`${prefix}.png`, `${prefix}-opening.png`],
          painted,
          ...overflow,
          contrast: await textContrast(),
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
        await page.reload({ waitUntil: "networkidle" });
        assert.equal(
          await page.evaluate(() => document.documentElement.dataset.theme),
          scheme === "light" ? "dark" : "light",
        );
        assert.equal(
          await page
            .locator("main")
            .evaluate((el) => getComputedStyle(el).backgroundColor),
          scheme === "light" ? "rgb(24, 26, 32)" : "rgb(255, 255, 255)",
        );
        await schemeButton.focus();
        await page.keyboard.press("Enter");
        await page.reload({ waitUntil: "networkidle" });
        assert.equal(
          await page.evaluate(() => document.documentElement.dataset.theme),
          scheme,
        );
        if (name === "ember") {
          for (const summary of await page
            .locator("details.class > summary")
            .all()) {
            await summary.focus();
            await page.keyboard.press("Enter");
            assert.equal(
              await summary.evaluate((el) => el.parentElement.open),
              true,
            );
            const expanded = await noOverflow();
            evidence.push({
              sourceHead,
              surface: name,
              scheme,
              width,
              expanded: await summary.innerText(),
              contrast: await textContrast(),
              ...expanded,
            });
          }
          await page.locator(".arch").focus();
          assert.notEqual(
            await page
              .locator(".arch")
              .evaluate((el) => getComputedStyle(el).outlineStyle),
            "none",
          );
          const diagramScreenshot = `ember-diagram-${scheme}-${width}.png`;
          await page.screenshot({
            path: `${output}/${diagramScreenshot}`,
            fullPage: true,
          });
          evidence.push({
            sourceHead,
            surface: "ember-diagram",
            scheme,
            width,
            height: 1000,
            screenshots: [diagramScreenshot],
            ...(await noOverflow()),
          });
        }
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
        .getByRole("link", { name: "Ember fixture", exact: true })
        .click();
      await page.locator(".ember-sheet").waitFor();
      assert.equal(
        await page.locator(".topbar, .ember-site, .md-nav").count(),
        0,
      );
      await noOverflow();
      assert.equal(await page.locator("button.scheme").count(), 1);
      const emberPaint = await page.locator(".ember-sheet").evaluate((el) => ({
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
        await page.locator(".ember-sheet").evaluate((el) => ({
          background: getComputedStyle(el).backgroundColor,
          font: getComputedStyle(el).fontFamily,
        })),
        emberPaint,
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
        sourceHead,
        scheme,
        width,
        navigation: "home / CV / engineering / Ember sheet / legacy Nav / home",
        themePersistence: "pass",
        keyboardFocus: "pass",
        emptyBanner: "pass",
        emberFreshAndNavigationPaint: emberPaint,
        errors,
      });
      assert.deepEqual(errors, []);
      await context.close();
    }
  }
} finally {
  await browser.close();
  await new Promise((resolve) => server.httpServer.close(resolve));
  await writeFile(
    `${output}/checks.json`,
    JSON.stringify(evidence, null, 2) + "\n",
  );
}
console.log(`PASS: ${evidence.length} capture/navigation records in ${output}`);
