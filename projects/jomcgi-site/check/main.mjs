import { client, checkLinks, tier1 } from "./tier1.mjs";
import { tier2 } from "./browser.mjs";
import { scopeFor } from "./scope.mjs";

const [stage, dist = "/app/site/web/dist"] = process.argv.slice(2);
const origin = { dev: "https://next.jomcgi.dev", prod: "https://jomcgi.dev" }[
  stage
];
if (!origin) throw new Error("usage: site-check <dev|prod> [dist]");
const deadline = setTimeout(() => {
  console.error("site-check exceeded its 180 second deadline");
  process.exit(1);
}, 180_000);
const http = client(origin);
try {
  const pages = await tier1({
    origin,
    dist,
    preview: stage === "dev",
    get: http.get,
    scope: scopeFor(stage),
  });
  await checkLinks(http.get, await tier2(pages));
  console.log(`site-check verified ${origin}: both tiers passed`);
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  await http.close();
  clearTimeout(deadline);
}
