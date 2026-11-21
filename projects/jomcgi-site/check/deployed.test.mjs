import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { checkPage, launchBrowser } from "./browser.mjs";
import { serveRelease, serveFixturePhoto } from "./release-fixture.mjs";
import { checkReleaseStage } from "./smoke.mjs";
import { test } from "node:test";
import { resolve } from "node:path";
import { isolateBrowserCache } from "./fixture.mjs";

test(
  "both tiers verify dev and the routed production pages",
  // Exercise two Jobs in sequence, each with the deployed 300-second budget.
  { timeout: 600_000 },
  async (t) => {
    isolateBrowserCache(t);
    const dist = resolve(process.env.SITE_CHECK_DIST ?? "../web/dist");
    for (const stage of ["dev", "prod"]) {
      await t.test(stage, { timeout: 300_000 }, async () => {
        await checkReleaseStage(dist, stage);
      });
    }
  },
);
test("public trip day loads its display derivative", async (t) => {
  isolateBrowserCache(t);
  const sources = JSON.parse(
    readFileSync(new URL("../data/sources.json", import.meta.url)),
  );
  sources.sections.trips.enabled = true;
  const server = await serveRelease(
    resolve(process.env.SITE_CHECK_DIST ?? "../web/dist"),
    "dev",
    { snapshotSources: sources },
  );
  t.after(() => server.close());
  const browser = await launchBrowser();
  t.after(() => browser.close());
  const requested = [];
  await checkPage(browser, {
    url: server.origin + "/trips/fixture-trip/day/1",
    servePhoto(route) {
      requested.push(route.request().url());
      return serveFixturePhoto(route);
    },
  });
  assert.ok(
    requested.includes(
      "https://factory-data.jomcgi.dev/trips/photos/img_fixture.jpg/display.webp",
    ),
  );
});
