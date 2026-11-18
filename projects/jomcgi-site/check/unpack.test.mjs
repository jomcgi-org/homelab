import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createReadStream, existsSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { createBrotliDecompress } from "node:zlib";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const chromium = pathToFileURL(require.resolve("@sparticuz/chromium"));
const fixture = new URL("./fixture.mjs", import.meta.url);

function unpack() {
  const script = `
    import { test } from "node:test";
    import { createHash } from "node:crypto";
    import { createReadStream } from "node:fs";
    import chrome from ${JSON.stringify(chromium.href)};
    import { isolateBrowserCache } from ${JSON.stringify(fixture.href)};
    test("cold extraction", async (t) => {
      isolateBrowserCache(t);
      const path = await chrome.executablePath();
      const hash = createHash("sha256");
      for await (const chunk of createReadStream(path)) hash.update(chunk);
      console.log("UNPACK " + JSON.stringify({path, hash: hash.digest("hex")}));
    });
  `;
  return new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      ["--input-type=module", "-e", script],
      {
        env: { ...process.env },
        timeout: 30_000,
      },
    );
    let output = "";
    child.stdout.on("data", (chunk) => (output += chunk));
    child.stderr.on("data", (chunk) => (output += chunk));
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0)
        return reject(new Error(`unpack exited ${code}: ${output}`));
      const result = output.match(/UNPACK (\{[^\n]+\})/);
      if (!result) return reject(new Error(`missing unpack result: ${output}`));
      resolve(JSON.parse(result[1]));
    });
  });
}

test("parallel browser tests unpack complete binaries into separate caches", async () => {
  const expected = createHash("sha256");
  const archive = new URL("../bin/chromium.br", chromium);
  for await (const chunk of createReadStream(archive).pipe(
    createBrotliDecompress(),
  )) {
    expected.update(chunk);
  }
  const digest = expected.digest("hex");
  const [first, second] = await Promise.all([unpack(), unpack()]);
  assert.notEqual(first.path, second.path);
  assert.equal(first.hash, digest);
  assert.equal(second.hash, digest);
  assert.equal(existsSync(first.path), false, "first test cache was removed");
  assert.equal(existsSync(second.path), false, "second test cache was removed");
});
