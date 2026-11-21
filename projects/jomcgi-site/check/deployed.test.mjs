import { test } from "node:test";
import { resolve } from "node:path";
import { client, checkLinks, tier1 } from "./tier1.mjs";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { checkPage, launchBrowser, tier2 } from "./browser.mjs";
import { isolateBrowserCache } from "./fixture.mjs";
import { serveRelease, serveFixturePhoto } from "./release-fixture.mjs";
import { scopeFor } from "./scope.mjs";

// The dev release includes Ships before the edge route moves from the hub.
const snapshot = "snapshot-" + "a".repeat(64) + ".json";
const shipsFiles = new Map([
  [
    "https://factory-data.jomcgi.dev/ships/latest.json",
    { snapshot, heat: "heat-" + "b".repeat(64) + ".json" },
  ],
  [
    "https://factory-data.jomcgi.dev/ships/" + snapshot,
    { generated_at: "2026-11-19T12:00:00+00:00", vessels: [] },
  ],
  [
    "https://tiles.openfreemap.org/styles/liberty",
    { version: 8, sources: {}, layers: [] },
  ],
]);
async function serveShipsRequest(route) {
  const json = shipsFiles.get(route.request().url());
  await route.fulfill({
    status: json ? 200 : 404,
    headers: { "Access-Control-Allow-Origin": "*" },
    json: json ?? {},
  });
}

test(
  "both tiers verify dev and the routed production pages",
  // Exercise two Jobs in sequence, each with the deployed 300-second budget.
  { timeout: 600_000 },
  async (t) => {
    isolateBrowserCache(t);
    const dist = resolve(process.env.SITE_CHECK_DIST ?? "../web/dist");
    for (const stage of ["dev", "prod"]) {
      await t.test(stage, { timeout: 300_000 }, async () => {
        const server = await serveRelease(dist, stage);
        const http = client(server.origin);
        try {
          const pages = await tier1({
            origin: server.origin,
            dist,
            preview: stage === "dev",
            get: http.get,
            scope: scopeFor(stage),
          });
          await checkLinks(
            http.get,
            await tier2(pages, {
              checkMaps: false,
              serveShipsRequest,
              servePhoto: serveFixturePhoto,
            }),
          );
        } finally {
          await http.close();
          await server.close();
        }
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
