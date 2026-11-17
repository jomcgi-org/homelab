// Compare the built file to the hub's real GET handler, including indentation
// and the trailing newline. Run after pnpm build: node marketplace.test.mjs dist
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import vm from "node:vm";

const hub = new URL("../../monolith/frontend/src/", import.meta.url);
const source = (path) => readFileSync(new URL(path, hub), "utf8");
const script = [
  source("lib/public/seo.js"),
  source("lib/cache-headers.js"),
  source("routes/public/scotland/scotland-data.js"),
  source("routes/public/scotland/marketplace.json/+server.js"),
]
  .join("\n")
  .replace(/^import[\s\S]*?;\n/gm, "")
  .replace(/^export /gm, "");
const response = vm.runInNewContext(`${script}\nGET()`, { Response });
const actual = readFileSync(join(process.argv[2], "scotland/marketplace.json"));
assert.deepEqual(actual, Buffer.from(await response.text()));
console.log("ok  Scotland marketplace matches the hub byte for byte");
