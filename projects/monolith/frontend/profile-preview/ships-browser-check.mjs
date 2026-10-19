import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { execFile, execFileSync } from "node:child_process";
import { promisify } from "node:util";
import { heat, vesselTrack } from "../test/ships-fixtures.js";

const { chromium } = await import(process.argv[2]);
const output = process.argv[3];
const providerCache = new Map();
const curl = promisify(execFile);
// The CI step starts Vite in parallel with browser startup.
for (let attempt = 0; attempt < 100; attempt++) {
  if (
    await fetch("http://127.0.0.1:4173")
      .then((r) => r.ok)
      .catch(() => false)
  )
    break;
  if (attempt === 99) throw new Error("Ships preview server did not start");
  await new Promise((resolve) => setTimeout(resolve, 200));
}
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  args: [
    "--no-sandbox",
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
  ],
});
const receipt = {
  head:
    process.env.FACTORY_PREVIEW_SHA ??
    execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim(),
  fixture:
    "Production route/layouts and real MapLibre, synthetic AIS data; existing OpenFreeMap tiles, sprites and glyphs",
  views: [],
};
try {
  for (const width of [390, 1440]) {
    for (const scheme of ["light", "dark"]) {
      const context = await browser.newContext({
        viewport: { width, height: 1000 },
        colorScheme: scheme,
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", (error) => {
        errors.push(error.message);
        console.error(error.message);
      });
      await page.clock.setFixedTime(new Date("2026-10-19T12:00:00Z"));
      await page.addInitScript(
        (scheme) => localStorage.setItem("td-theme", scheme),
        scheme,
      );
      await page.route("https://fonts.googleapis.com/**", (route) =>
        route.fulfill({ contentType: "text/css", body: "" }),
      );
      // This guest's browser cannot use its egress proxy directly. Relay only
      // the existing public provider through curl without altering its bytes.
      await page.route("https://tiles.openfreemap.org/**", async (route) => {
        try {
          const url = route.request().url();
          if (!providerCache.has(url))
            providerCache.set(
              url,
              curl(
                "curl",
                [
                  "--fail",
                  "--silent",
                  "--show-error",
                  "--location",
                  "--max-time",
                  "30",
                  url,
                ],
                { maxBuffer: 20e6, encoding: "buffer" },
              ).then((result) => result.stdout),
            );
          const body = await providerCache.get(url);
          await route.fulfill({
            status: 200,
            body,
            headers: { "access-control-allow-origin": "*" },
          });
        } catch {
          await route.abort();
        }
      });
      await page.route("**/app/ships/heat", (route) =>
        route.fulfill({ json: heat }),
      );
      await page.route("**/app/ships/track/*", (route) =>
        route.fulfill({
          json: vesselTrack(route.request().url().split("/").pop()),
        }),
      );
      await page.goto("http://127.0.0.1:4173/app/ships", {
        waitUntil: "networkidle",
      });
      await page
        .waitForFunction(
          () =>
            window.shipsMap?.getLayer("vessels") &&
            window.shipsMap.areTilesLoaded() &&
            window.shipsMap.queryRenderedFeatures({ layers: ["vessels"] })
              .length > 20,
        )
        .catch(async (error) => {
          console.error(
            await page.evaluate(() => ({
              errors: window.shipsMapErrors,
              map: Boolean(window.shipsMap),
              body: document.body.innerText.slice(0, 600),
            })),
          );
          await page.screenshot({ path: `${output}/failed-load.png` });
          throw error;
        });
      await page.evaluate(() => document.fonts.ready);
      const prefix = `ships-${scheme}-${width}`;
      const inspect = () =>
        page.evaluate(() => {
          const map = window.shipsMap;
          return {
            clientWidth: document.documentElement.clientWidth,
            scrollWidth: document.documentElement.scrollWidth,
            chrome: document.querySelectorAll(".chrome").length,
            schemes: document.querySelectorAll("button.scheme").length,
            vessels: map.queryRenderedFeatures({ layers: ["vessels"] }).length,
            basemap: map
              .queryRenderedFeatures()
              .filter((f) => !["vessels", "heat-fill"].includes(f.layer.id))
              .length,
            errors: window.shipsMapErrors,
            camera: { center: map.getCenter().toArray(), zoom: map.getZoom() },
            ink: map.getPaintProperty("ship-track-line", "line-color"),
            water: map.getPaintProperty("water", "fill-color"),
          };
        });
      const before = await inspect();
      assert.equal(before.chrome, 1);
      assert.equal(before.schemes, 1);
      assert.ok(
        before.scrollWidth <= before.clientWidth,
        JSON.stringify(before),
      );
      assert.ok(before.vessels > 20, JSON.stringify(before));
      assert.ok(
        before.basemap > 20,
        "Provider tiles must contain meaningful map content",
      );
      assert.deepEqual(before.errors, []);
      await page.screenshot({
        path: `${output}/${prefix}.png`,
        fullPage: true,
      });

      const cargo = page.getByRole("button", { name: "Cargo", exact: true });
      await cargo.click();
      const selection = await page.evaluate(() => {
        const map = window.shipsMap;
        const feature = map
          .queryRenderedFeatures({ layers: ["vessels"] })
          .find((f) => {
            const p = map.project(f.geometry.coordinates);
            return (
              p.x > 50 &&
              p.x < map.getCanvas().clientWidth - 100 &&
              p.y > 100 &&
              p.y < map.getCanvas().clientHeight - 250
            );
          });
        if (!feature)
          throw new Error("No unobscured vessel available to select");
        const p = map.project(feature.geometry.coordinates);
        const box = map.getCanvas().getBoundingClientRect();
        return { x: box.x + p.x, y: box.y + p.y };
      });
      await page.mouse.click(selection.x, selection.y);
      await page.getByText("8 track points", { exact: true }).waitFor();
      await page.screenshot({ path: `${output}/${prefix}-selected.png` });
      const selected = await page.evaluate(() => ({
        query: location.search,
        camera: window.shipsMap.getCenter().toArray(),
        track: window.shipsMap.getSource("ship-track")._data,
        filter: window.shipsMap.getFilter("vessels"),
      }));
      await page.keyboard.press("Tab");
      await page.locator("button.scheme").focus();
      assert.notEqual(
        await page
          .locator("button.scheme")
          .evaluate((e) => getComputedStyle(e).outlineStyle),
        "none",
      );
      await page.keyboard.press("Enter");
      await page.waitForFunction(
        (ink) =>
          window.shipsMap.getPaintProperty("ship-track-line", "line-color") !==
          ink,
        before.ink,
      );
      assert.deepEqual(
        await page.evaluate(() => ({
          query: location.search,
          camera: window.shipsMap.getCenter().toArray(),
          track: window.shipsMap.getSource("ship-track")._data,
          filter: window.shipsMap.getFilter("vessels"),
        })),
        selected,
      );
      await page.keyboard.press("Enter");
      await page.getByRole("button", { name: "Close vessel panel" }).click();
      await page.getByRole("button", { name: "Heat", exact: true }).click();
      await page.getByText("250+", { exact: true }).waitFor();
      await page.waitForFunction(
        () =>
          window.shipsMap.queryRenderedFeatures({ layers: ["heat-fill"] })
            .length > 20,
      );
      await page.screenshot({ path: `${output}/${prefix}-heat.png` });
      await page.reload({ waitUntil: "networkidle" });
      await page.waitForFunction(
        () =>
          window.shipsMap?.getLayer("heat-fill") &&
          window.shipsMap.areTilesLoaded(),
      );
      assert.equal(
        await page
          .getByRole("button", { name: "Heat", exact: true })
          .getAttribute("aria-pressed"),
        "true",
      );
      assert.equal(
        await page.evaluate(() => document.documentElement.dataset.theme),
        scheme,
      );
      await page.getByRole("button", { name: "Vessels", exact: true }).click();
      const zoom = await page.evaluate(() => window.shipsMap.getZoom());
      await page.getByRole("button", { name: "Zoom in", exact: true }).click();
      await page.waitForFunction(
        (zoom) => window.shipsMap.getZoom() > zoom,
        zoom,
      );
      const center = await page.evaluate(() =>
        window.shipsMap.getCenter().toArray(),
      );
      const bounds = await page.locator(".maplibregl-canvas").boundingBox();
      await page.mouse.move(
        bounds.x + bounds.width / 2,
        bounds.y + bounds.height / 2,
      );
      await page.mouse.down();
      await page.mouse.move(
        bounds.x + bounds.width / 2 + 50,
        bounds.y + bounds.height / 2 + 30,
        { steps: 8 },
      );
      await page.mouse.up();
      assert.notDeepEqual(
        await page.evaluate(() => window.shipsMap.getCenter().toArray()),
        center,
      );
      await page.locator(".maplibregl-ctrl-attrib-button").click();
      await page.getByText("OpenStreetMap", { exact: true }).waitFor();
      assert.deepEqual(errors, []);
      receipt.views.push({
        width,
        scheme,
        ...before,
        screenshots: [
          `${prefix}.png`,
          `${prefix}-selected.png`,
          `${prefix}-heat.png`,
        ],
        interactions:
          "filter, select, track, keyboard scheme, deselect, heat, URL reload, zoom, pan, attribution",
      });
      // Retained legacy CSS and client navigation must not contaminate either
      // surface. Compare the real Ember paint against a fresh load.
      const fresh = await context.newPage();
      await fresh.route("https://fonts.googleapis.com/**", (route) =>
        route.fulfill({ contentType: "text/css", body: "" }),
      );
      await fresh.goto("http://127.0.0.1:4173/ember", {
        waitUntil: "networkidle",
      });
      const paint = (p) =>
        p.locator(".ember-site").evaluate((e) => {
          const s = getComputedStyle(e);
          return {
            background: s.backgroundColor,
            color: s.color,
            font: s.fontFamily,
          };
        });
      const expected = await paint(fresh);
      await page.locator('.trail a[href="/"]').click();
      await page
        .getByRole("link", { name: "Legacy Ember fixture", exact: true })
        .click();
      assert.deepEqual(await paint(page), expected);
      await page
        .getByRole("link", { name: "Ships fixture", exact: true })
        .click();
      await page.waitForFunction(
        () =>
          window.shipsMap?.getLayer("vessels") &&
          window.shipsMap.areTilesLoaded(),
      );
      assert.equal(await page.locator("button.scheme").count(), 1);
      assert.ok(
        await page.evaluate(
          () =>
            document.documentElement.scrollWidth <=
            document.documentElement.clientWidth,
        ),
      );
      receipt.views.at(-1).navigation =
        "ships, home, legacy Ember, ships; legacy paint matches a fresh load";
      await fresh.close();
      await context.close();
    }
  }
} finally {
  await writeFile(
    `${output}/ships-checks.json`,
    JSON.stringify(receipt, null, 2),
  );
  await browser.close();
}
