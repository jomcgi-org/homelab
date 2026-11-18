// Run from a network that can reach both public storyboard origins.
import { createHash } from "node:crypto";
import { validate } from "./schema/validate.ts";

async function read(url) {
  const response = await fetch(url, {
    redirect: "error",
    signal: AbortSignal.timeout(15000),
  });
  if (response.status !== 200)
    throw new Error(`${url}: HTTP ${response.status}`);
  return Buffer.from(await response.arrayBuffer());
}

async function check(url, checkFreshness) {
  const manifest = JSON.parse(await read(url));
  if (!validate("manifest", manifest)) throw new Error("invalid v1 manifest");
  if (checkFreshness) {
    const age = Date.now() - Date.parse(manifest.generatedAt);
    if (!Number.isFinite(age) || age < 0 || age > 7 * 60_000)
      throw new Error(`manifest generated at ${manifest.generatedAt}`);
    return;
  }
  const paths = new Set([
    manifest.story,
    ...Object.values(manifest.goals),
    ...Object.values(manifest.issues),
    ...Object.values(manifest.sessions),
  ]);
  for (const path of paths) {
    const body = await read(new URL(path, url));
    if (
      path !== `objects/${createHash("sha256").update(body).digest("hex")}.json`
    )
      throw new Error(`digest mismatch: ${path}`);
  }
}

for (const [url, freshness] of [
  ["https://factory-data.jomcgi.dev/manifest.json", true],
  ["https://next.jomcgi.dev/data/factory/manifest.json", false],
]) {
  try {
    await check(url, freshness);
    console.log(`PASS ${url}`);
  } catch (error) {
    console.error(`FAIL ${url}: ${error.cause?.message ?? error.message}`);
    process.exitCode = 1;
  }
}
