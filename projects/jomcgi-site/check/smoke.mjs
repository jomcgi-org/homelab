import assert from "node:assert/strict";
import { client, checkLinks, tier1 } from "./tier1.mjs";
import { checkPage, launchBrowser, tier2 } from "./browser.mjs";
import { serveRelease } from "./release-fixture.mjs";
import { scopeFor } from "./scope.mjs";

// Invoked by the image test through the deployed launcher's --self-test mode.
export async function smoke(dist) {
  assert.equal(process.getuid(), 65532, "image must run as the Job's uid");
  assert.equal(
    process.env.SITE_CHECK_BROWSER,
    undefined,
    "use the packaged browser",
  );
  for (const stage of ["dev", "prod"]) {
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
      await checkLinks(http.get, await tier2(pages, { checkMaps: false }));
      console.log(`image smoke: ${stage} release passed both tiers`);
    } finally {
      await http.close();
      await server.close();
    }
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
