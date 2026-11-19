import { test } from "node:test";
import { resolve } from "node:path";
import { client, checkLinks, tier1 } from "./tier1.mjs";
import { tier2 } from "./browser.mjs";
import { isolateBrowserCache } from "./fixture.mjs";
import { serveRelease } from "./release-fixture.mjs";
import { scopeFor } from "./scope.mjs";

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
          await checkLinks(http.get, await tier2(pages, { checkMaps: false }));
        } finally {
          await http.close();
          await server.close();
        }
      });
    }
  },
);
