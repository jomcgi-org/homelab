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
