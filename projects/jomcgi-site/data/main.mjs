// Entry point of the data snapshot and the release fence (HOME-209), run by
// run.sh. It always runs; snapshot.mjs and fence.mjs hold the logic the tests
// import.
//   snapshot <output directory>
//     Env: R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY (read-only R2 token).
//   snapshot fence-check
//     Env: CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, WORKER, CHART_VERSION.
//     Exits 0 when this chart version may deploy, 10 when a different
//     release is live, and 1 on an error.
import { readFileSync } from "node:fs";
import { isCurrent, liveMessage, releaseMessage } from "./fence.mjs";
import { r2Getter, snapshot } from "./snapshot.mjs";

const need = (name) => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} must be set`);
  return value;
};

if (process.argv[2] === "fence-check") {
  const chart = need("CHART_VERSION");
  const live = await liveMessage({
    token: need("CLOUDFLARE_API_TOKEN"),
    account: need("CLOUDFLARE_ACCOUNT_ID"),
    worker: need("WORKER"),
  });
  const ok = isCurrent(live, chart);
  console.log(
    `live: ${live === null ? "no deployment" : JSON.stringify(live)}; ` +
      `this release: ${JSON.stringify(releaseMessage(chart))}; ` +
      (ok ? "may deploy" : "a different release is live"),
  );
  process.exit(ok ? 0 : 10);
}

const out = process.argv[2];
if (!out)
  throw new Error("usage: snapshot <output directory> | snapshot fence-check");
const sources = JSON.parse(
  readFileSync(new URL("./sources.json", import.meta.url), "utf8"),
);
const get = r2Getter(sources.endpoint, {
  accessKeyId: need("R2_ACCESS_KEY_ID"),
  secretAccessKey: need("R2_SECRET_ACCESS_KEY"),
});
for (const line of await snapshot(sources, get, out)) console.log(line);
