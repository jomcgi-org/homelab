import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
const { chromium } = await import(pathToFileURL(resolve(process.argv[2])).href);
const output = resolve(process.argv[3]);
await mkdir(output, { recursive: true });
const base = process.env.CAMPSITES_PREVIEW_URL ?? "http://127.0.0.1:4173";
let browser;
const evidence = [];
try {
  for (const width of [390, 1440]) {
    for (const scheme of ["light", "dark"]) {
      // Isolate software WebGL resources between viewport/scheme scenarios.
      if (browser) await browser.close();
      browser = await chromium.launch({
        headless: true,
        args: ["--enable-unsafe-swiftshader"],
      });
      const context = await browser.newContext({
        viewport: { width, height: 1000 },
        colorScheme: scheme,
        reducedMotion: "reduce",
      });
      await context.route(
        /https:\/\/fonts\.(googleapis|gstatic)\.com\//,
        (route) => route.fulfill({ contentType: "text/css", body: "" }),
      );
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => errors.push(error.message));
      await page.clock.setFixedTime(new Date("2026-10-19T12:00:00Z"));
      const ready = () =>
        page.waitForFunction(
          () =>
            window.campsitesMap?.getLayer("parks") &&
            window.campsitesMap.loaded(),
          null,
          { timeout: 60000 },
        );
      const overflow = async (state) => {
        const result = await page.evaluate(() => ({
          viewport: innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
        }));
        assert.ok(
          result.scrollWidth <= result.viewport,
          `${state}: ${JSON.stringify(result)}`,
        );
        return { state, ...result };
      };
      const sourceIds = () =>
        page.evaluate(async () =>
          (await window.campsitesMap.getSource("parks").getData()).features.map(
            (f) => f.properties.id,
          ),
        );
      await page.goto(`${base}/app/campsites`);
      await ready();
      await page.evaluate(() => document.fonts.ready);
      const checks = [await overflow("closed")];
      assert.equal(await page.locator("button.scheme").count(), 1);
      assert.equal(await page.locator(".md-nav").count(), 0);
      assert.equal(
        await page.locator(".nights-toggle").getAttribute("aria-expanded"),
        "false",
      );
      assert.deepEqual(await sourceIds(), [
        "birch",
        "alpha",
        "cedar",
        "dormant",
      ]);
      // Save identity and real renderer state before a keyboard scheme change.
      await page.evaluate(() => {
        const map = window.campsitesMap;
        window.beforeTheme = {
          map,
          source: map.getSource("parks"),
          center: map.getCenter().toArray(),
          zoom: map.getZoom(),
          layers: map.getStyle().layers.map((l) => l.id),
          paint: map.getPaintProperty("parks", "circle-color"),
        };
        map.setLayoutProperty("weather-heat", "visibility", "none");
      });
      const toggle = page.locator("button.scheme");
      await toggle.focus();
      assert.notEqual(
        await toggle.evaluate((el) => getComputedStyle(el).outlineStyle),
        "none",
      );
      await page.keyboard.press("Enter");
      await page.waitForFunction(
        () =>
          JSON.stringify(window.beforeTheme.paint) !==
          JSON.stringify(
            window.campsitesMap.getPaintProperty("parks", "circle-color"),
          ),
      );
      assert.equal(
        await page.evaluate(() => {
          const before = window.beforeTheme,
            map = window.campsitesMap;
          return (
            map === before.map &&
            map.getSource("parks") === before.source &&
            map.getZoom() === before.zoom &&
            JSON.stringify(map.getCenter().toArray()) ===
              JSON.stringify(before.center) &&
            JSON.stringify(map.getStyle().layers.map((l) => l.id)) ===
              JSON.stringify(before.layers) &&
            map.getLayoutProperty("weather-heat", "visibility") === "none"
          );
        }),
        true,
      );
      await page.keyboard.press("Enter");
      await page.evaluate(() =>
        window.campsitesMap.setLayoutProperty(
          "weather-heat",
          "visibility",
          "visible",
        ),
      );
      assert.equal(
        await page.evaluate(() => localStorage.getItem("td-theme")),
        scheme,
      );
      await page.reload();
      await ready();
      assert.equal(
        await page.evaluate(() => document.documentElement.dataset.theme),
        scheme,
      );
      await page.locator(".nights-toggle").click();
      await page.locator(".collapse-btn").click();
      checks.push(await overflow("panels-open"));
      assert.equal(await page.locator(".night-btn:disabled").count(), 11);
      await page
        .locator(".nights-act")
        .filter({ hasText: /^Clear$/ })
        .click();
      assert.equal(new URL(page.url()).searchParams.get("nights"), "");
      assert.deepEqual(await sourceIds(), []);
      await page.locator(".night-btn").nth(1).click();
      assert.equal(
        new URL(page.url()).searchParams.get("nights"),
        "2026-10-24",
      );
      assert.deepEqual(await sourceIds(), ["alpha", "birch"]);
      await page.reload();
      await ready();
      assert.equal(
        await page.locator(".nights-toggle-summary").textContent(),
        "1 night",
      );
      await page.locator(".nights-toggle").click();
      await page.locator(".nights-act").filter({ hasText: /^All$/ }).click();
      await page.locator(".collapse-btn").click();
      await page
        .locator(".seg")
        .filter({ hasText: /^Nights$/ })
        .click();
      assert.equal(
        await page.locator(".r-name").first().textContent(),
        "Alpha Lake",
      );
      await page
        .locator(".seg")
        .filter({ hasText: /^Sky score$/ })
        .click();
      assert.equal(
        await page.locator(".r-name").first().textContent(),
        "Birch Lake",
      );
      await page
        .locator(".seg")
        .filter({ hasText: /^Name$/ })
        .click();
      await page.locator(".check-input").check();
      assert.deepEqual(await sourceIds(), ["alpha", "birch"]);
      await page.locator(".check-input").uncheck();
      await page.keyboard.press("Tab");
      await page.locator(".row").first().focus();
      assert.notEqual(
        await page
          .locator(".row")
          .first()
          .evaluate((el) => getComputedStyle(el).outlineStyle),
        "none",
      );
      await page.keyboard.press("Enter");
      await page.waitForFunction(() => !window.campsitesMap.isMoving());
      assert.equal(
        await page.locator(".detail-park").textContent(),
        "Alpha Lake",
      );
      // Pan/zoom the real camera, then click the actual rendered park pin.
      await page.evaluate(() =>
        window.campsitesMap.jumpTo({ center: [-122.1, 50.9], zoom: 7 }),
      );
      await ready();
      await page.locator(".maplibregl-canvas").scrollIntoViewIfNeeded();
      const canvasBox = await page.locator(".maplibregl-canvas").boundingBox();
      const centerBeforeDrag = await page.evaluate(() =>
        window.campsitesMap.getCenter().toArray(),
      );
      await page.mouse.move(
        canvasBox.x + canvasBox.width / 2,
        canvasBox.y + canvasBox.height / 3,
      );
      await page.mouse.down();
      await page.mouse.move(
        canvasBox.x + canvasBox.width / 2 + 55,
        canvasBox.y + canvasBox.height / 3 + 30,
        { steps: 6 },
      );
      await page.mouse.up();
      await page.waitForFunction(() => !window.campsitesMap.isMoving());
      assert.notDeepEqual(
        await page.evaluate(() => window.campsitesMap.getCenter().toArray()),
        centerBeforeDrag,
      );
      const point = await page.evaluate(() => {
        const map = window.campsitesMap,
          point = map.project([-122.1, 50.9]);
        const box = map.getCanvas().getBoundingClientRect();
        return { x: box.x + point.x, y: box.y + point.y };
      });
      await page.mouse.click(point.x, point.y);
      assert.equal(
        await page.locator(".detail-park").textContent(),
        "Birch Lake",
      );
      assert.equal(
        await page.locator(".row.selected .r-name").textContent(),
        "Birch Lake",
      );
      assert.ok(
        (
          await page.locator(".maplibregl-popup-content").textContent()
        ).includes("Birch Lake"),
      );
      for (let flip = 0; flip < 2; flip++) {
        await toggle.click();
        await ready();
        assert.equal(
          await page.locator(".row.selected .r-name").textContent(),
          "Birch Lake",
        );
        assert.ok(
          (
            await page.locator(".maplibregl-popup-content").textContent()
          ).includes("Birch Lake"),
        );
        assert.equal(
          await page.evaluate(
            async () =>
              (
                await window.campsitesMap.getSource("parks").getData()
              ).features.find((f) => f.properties.id === "birch").properties
                .sel,
          ),
          1,
        );
      }
      const zoom = await page.evaluate(() => window.campsitesMap.getZoom());
      await page.locator(".maplibregl-ctrl-zoom-in").click();
      await page.waitForFunction(
        (z) =>
          window.campsitesMap.getZoom() > z && !window.campsitesMap.isMoving(),
        zoom,
      );
      if (width === 390) {
        await page.locator(".legend-toggle").click();
        assert.equal(await page.locator(".legend").isVisible(), true);
        await page.locator(".detail-park").click();
        assert.equal(await page.locator(".legend").isVisible(), false);
        await page.locator(".legend-toggle").click();
      }
      checks.push(await overflow("selection-and-legend"));
      // Frame all synthetic parks for the evidence while preserving selection.
      await page.evaluate(() =>
        window.campsitesMap.fitBounds(
          [
            [-126, 49],
            [-121, 54],
          ],
          { padding: 56, duration: 0 },
        ),
      );
      await ready();
      await page.evaluate(() => window.scrollTo(0, 0));
      const painted = await page
        .locator("main.campsites-page")
        .evaluate((el) => ({
          sheet: getComputedStyle(el).backgroundColor,
          ink: getComputedStyle(el).color,
        }));
      assert.equal(
        painted.sheet,
        scheme === "light" ? "rgb(255, 255, 255)" : "rgb(24, 26, 32)",
      );
      const screenshot = `campsites-${scheme}-${width}.png`;
      await page.screenshot({
        path: `${output}/${screenshot}`,
        fullPage: true,
      });
      // Navigation keeps loaded stylesheets and exercises the production layouts.
      await page.evaluate(() => {
        window.navigationSentinel = true;
      });
      await page
        .getByRole("link", { name: "Engineering fixture", exact: true })
        .click();
      await page.locator("main.eng-page").waitFor();
      assert.equal(await page.locator(".md-nav").count(), 0);
      await overflow("engineering");
      await page
        .getByRole("link", { name: "Legacy Ember fixture", exact: true })
        .click();
      await page.locator(".ember-site").waitFor();
      assert.equal(await page.locator(".td").count(), 0);
      const legacyPaint = await page.locator(".ember-site").evaluate((el) => ({
        color: getComputedStyle(el).color,
        background: getComputedStyle(el).backgroundColor,
      }));
      await overflow("legacy-ember");
      await page
        .getByRole("link", { name: "Legacy Nav fixture", exact: true })
        .click();
      assert.equal(await page.locator(".md-nav").count(), 1);
      assert.equal(await page.locator("button.scheme").count(), 0);
      await page
        .getByRole("link", { name: "Campsites fixture", exact: true })
        .click();
      await ready();
      assert.equal(await page.evaluate(() => window.navigationSentinel), true);
      assert.equal(await page.locator(".md-nav").count(), 0);
      assert.equal(await page.locator("button.scheme").count(), 1);
      checks.push(await overflow("return-to-campsites"));
      const fresh = await context.newPage();
      await fresh.goto(`${base}/ember`);
      assert.deepEqual(
        await fresh.locator(".ember-site").evaluate((el) => ({
          color: getComputedStyle(el).color,
          background: getComputedStyle(el).backgroundColor,
        })),
        legacyPaint,
      );
      await fresh.close();
      await page.goto(`${base}/app/campsites?empty`);
      await ready();
      await page.locator(".collapse-btn").click();
      assert.equal(
        await page.locator(".empty").textContent(),
        "No parks match the current filters.",
      );
      assert.deepEqual(errors, []);
      evidence.push({
        width,
        scheme,
        screenshot,
        checks,
        painted,
        errors,
        map: "Real MapLibre with OpenFreeMap liberty sources and synthetic park data",
      });
      await context.close();
      console.log(`PASS ${width} ${scheme}`);
    }
  }
  await writeFile(
    `${output}/checks.json`,
    `${JSON.stringify(evidence, null, 2)}\n`,
  );
} finally {
  await browser?.close();
}
