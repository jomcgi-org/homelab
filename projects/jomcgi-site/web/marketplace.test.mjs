// Compare the built file to the hub's real GET handler, including indentation
// and the trailing newline. Run after pnpm build: node marketplace.test.mjs dist
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { evaluateHub } from "./hub-fixture.mjs";

const response = evaluateHub(
  [
    "lib/public/seo.js",
    "lib/cache-headers.js",
    "routes/public/scotland/scotland-data.js",
    "routes/public/scotland/marketplace.json/+server.js",
  ],
  "GET()",
);
const actual = readFileSync(join(process.argv[2], "scotland/marketplace.json"));
assert.deepEqual(actual, Buffer.from(await response.text()));
console.log("ok  Scotland marketplace matches the hub byte for byte");
