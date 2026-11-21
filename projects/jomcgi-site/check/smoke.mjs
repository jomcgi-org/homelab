import assert from "node:assert/strict";
import { client, checkLinks, tier1 } from "./tier1.mjs";
import { checkPage, launchBrowser, tier2 } from "./browser.mjs";
import { serveRelease } from "./release-fixture.mjs";
import { scopeFor } from "./scope.mjs";

// The dev release includes Ships before the edge route moves from the hub.
const shipsSnapshot = "snapshot-" + "a".repeat(64) + ".json";
const shipsFiles = new Map([
  [
    "https://factory-data.jomcgi.dev/ships/latest.json",
    { snapshot: shipsSnapshot, heat: "heat-" + "b".repeat(64) + ".json" },
  ],
  [
    "https://factory-data.jomcgi.dev/ships/" + shipsSnapshot,
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

export async function checkReleaseStage(dist, stage) {
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
      await tier2(pages, { checkMaps: false, serveShipsRequest }),
    );
  } finally {
    await http.close();
    await server.close();
  }
}

// Invoked by the image test through the deployed launcher's --self-test mode.
export async function smoke(dist) {
  assert.equal(process.getuid(), 65532, "image must run as the Job's uid");
  assert.equal(
    process.env.SITE_CHECK_BROWSER,
    undefined,
    "use the packaged browser",
  );
  for (const stage of ["dev", "prod"]) {
    await checkReleaseStage(dist, stage);
    console.log(`image smoke: ${stage} release passed both tiers`);
  }
  const server = await serveRelease(dist, "dev", { contrast: true });
  try {
    const browser = await launchBrowser();
    try {
      console.log(
        `image smoke: Chromium ${browser.version()}, uid ${process.getuid()}`,
      );
      await assert.rejects(
        checkPage(browser, { url: `${server.origin}/` }),
        /axe color-contrast/,
      );
      console.log("image smoke: rendered CSS contrast violation rejected");
    } finally {
      await browser.close();
    }
  } finally {
    await server.close();
  }
}
