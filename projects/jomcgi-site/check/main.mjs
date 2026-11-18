import { client, checkLinks, tier1 } from "./tier1.mjs";
import { tier2 } from "./browser.mjs";
import { scopeFor } from "./scope.mjs";

const [stage, dist = "/app/site/web/dist"] = process.argv.slice(2);
const origin = { dev: "https://next.jomcgi.dev", prod: "https://jomcgi.dev" }[
  stage
];
if (!origin && stage !== "--self-test")
  throw new Error("usage: site-check <dev|prod|--self-test> [dist]");
// The Job checks one stage in 300 seconds. The image self-test checks two
// stages and the contrast case, so it gets the budget of three.
const seconds = stage === "--self-test" ? 540 : 300;
const deadline = setTimeout(() => {
  console.error(`site-check exceeded its ${seconds} second deadline`);
  process.exit(1);
}, seconds * 1000);
const http = origin ? client(origin) : null;
try {
  if (stage === "--self-test") {
    const { smoke } = await import("./smoke.mjs");
    await smoke(dist);
  } else {
    const pages = await tier1({
      origin,
      dist,
      preview: stage === "dev",
      get: http.get,
      scope: scopeFor(stage),
    });
    await checkLinks(http.get, await tier2(pages));
    console.log(`site-check verified ${origin}: both tiers passed`);
  }
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  await http?.close();
  clearTimeout(deadline);
}
