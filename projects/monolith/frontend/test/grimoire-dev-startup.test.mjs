import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = fileURLToPath(new URL("..", import.meta.url));
const clientEntry = "/src/lib/public/factory/markdown.js";
const clientDependencies = ["marked"];

async function stop(child, exited) {
  child.kill("SIGTERM");
  const deadline = setTimeout(() => child.kill("SIGKILL"), 10000);
  try {
    await exited;
  } finally {
    clearTimeout(deadline);
  }
}

for (let attempt = 1; attempt <= 3; attempt += 1) {
  test(`cold start ${attempt} commits dependencies before readiness`, async () => {
    const directory = await mkdtemp(
      path.join(tmpdir(), "grimoire-startup-test-"),
    );
    const cache = path.join(directory, "cache");
    const readyFile = path.join(directory, "ready.json");
    const probe = createServer();
    probe.listen(0, "127.0.0.1");
    await once(probe, "listening");
    const port = probe.address().port;
    await new Promise((resolve) => probe.close(resolve));
    const child = spawn(
      process.execPath,
      [
        "test/grimoire-dev-server.mjs",
        "--cache-dir",
        cache,
        "--ready-file",
        readyFile,
        "--port",
        String(port),
      ],
      { cwd: root, env: { ...process.env, VITE_GRIMOIRE_LOCAL: "true" } },
    );
    let output = "";
    child.stdout.on("data", (data) => {
      output += data;
    });
    child.stderr.on("data", (data) => {
      output += data;
    });
    const exited = once(child, "exit");
    try {
      let ready;
      for (let poll = 0; poll < 100; poll += 1) {
        assert.equal(child.exitCode, null, output);
        try {
          ready = JSON.parse(await readFile(readyFile, "utf8"));
          break;
        } catch (error) {
          if (error.code !== "ENOENT") throw error;
          await delay(100);
        }
      }
      assert.ok(ready, `Startup readiness timed out:\n${output}`);
      for (const dependency of clientDependencies) {
        assert.ok(
          ready.optimized.includes(dependency),
          `${dependency} missing at readiness`,
        );
      }
      const metadataFile = path.join(cache, "deps", "_metadata.json");
      const before = await readFile(metadataFile, "utf8");
      assert.deepEqual(
        Object.keys(JSON.parse(before).optimized).sort(),
        ready.optimized.sort(),
      );
      // Fetch the entry graph exactly as the browser does. Every optimized
      // import must already be usable; these requests must not change its hash.
      const pending = [clientEntry];
      const seen = new Set();
      while (pending.length) {
        const url = pending.shift();
        if (seen.has(url)) continue;
        seen.add(url);
        const response = await fetch(new URL(url, ready.url), {
          signal: AbortSignal.timeout(10000),
        });
        assert.equal(response.status, 200, `${url}: ${output}`);
        const code = await response.text();
        for (const match of code.matchAll(
          /(?:from\s*|import\s*)["']([./][^"']+)["']/g,
        )) {
          pending.push(new URL(match[1], new URL(url, ready.url)).href);
        }
      }
      assert.ok(
        seen.size > clientDependencies.length,
        "Client imports were not traversed",
      );
      assert.ok(
        [...seen].some((url) => /\/marked\.js\?v=/.test(url)),
        "The renderer's optimized marked import was not fetched",
      );
      assert.equal(await readFile(metadataFile, "utf8"), before);
      assert.doesNotMatch(
        output,
        /new dependencies optimized|optimized dependencies changed/,
      );
    } finally {
      await stop(child, exited);
      await rm(directory, { recursive: true, force: true });
    }
  });
}

for (const broken of [false, true]) {
  test(`client import fixture ${broken ? "rejects a broken import" : "publishes readiness"}`, async () => {
    const directory = await mkdtemp(
      path.join(tmpdir(), "grimoire-broken-startup-"),
    );
    const readyFile = path.join(directory, "ready.json");
    await mkdir(path.join(directory, "src/lib/public/factory"), {
      recursive: true,
    });
    const dependency = path.join(directory, "node_modules/startup-dependency");
    await mkdir(dependency, { recursive: true });
    await writeFile(
      path.join(dependency, "package.json"),
      JSON.stringify({
        name: "startup-dependency",
        type: "module",
        main: "index.js",
      }),
    );
    await writeFile(
      path.join(dependency, "index.js"),
      "export const ready = true;",
    );
    // Scan a healthy dependency independently. The broken client entry is only
    // checked by the readiness transform, so skipping it must fail this test.
    await writeFile(
      path.join(directory, "src/scan.js"),
      'import "startup-dependency";',
    );
    await writeFile(
      path.join(directory, clientEntry.slice(1)),
      'import "startup-dependency";' +
        (broken ? '\nimport "missing-grimoire-startup-dependency";' : ""),
    );
    await writeFile(
      path.join(directory, "vite.config.mjs"),
      'export default { optimizeDeps: { entries: ["src/scan.js"] } };',
    );
    const child = spawn(
      process.execPath,
      [
        path.join(root, "test", "grimoire-dev-server.mjs"),
        "--cache-dir",
        path.join(directory, "cache"),
        "--ready-file",
        readyFile,
        "--port",
        "0",
      ],
      { cwd: directory },
    );
    let output = "";
    child.stdout.on("data", (data) => {
      output += data;
    });
    child.stderr.on("data", (data) => {
      output += data;
    });
    const exited = once(child, "exit");
    try {
      if (!broken) {
        let ready;
        for (let poll = 0; poll < 100; poll += 1) {
          assert.equal(child.exitCode, null, output);
          try {
            ready = JSON.parse(await readFile(readyFile, "utf8"));
            break;
          } catch (error) {
            if (error.code !== "ENOENT") throw error;
            await delay(100);
          }
        }
        assert.ok(
          ready,
          `Healthy fixture did not publish readiness:\n${output}`,
        );
        assert.ok(ready.optimized.includes("startup-dependency"));
        const response = await fetch(new URL(clientEntry, ready.url), {
          signal: AbortSignal.timeout(10000),
        });
        assert.equal(response.status, 200, output);
        assert.match(await response.text(), /startup-dependency/);
        return;
      }
      const [code] = await once(child, "exit", {
        signal: AbortSignal.timeout(10000),
      });
      assert.notEqual(code, 0, output);
      assert.match(output, /missing-grimoire-startup-dependency/);
      await assert.rejects(readFile(readyFile), { code: "ENOENT" });
    } finally {
      await stop(child, exited);
      await rm(directory, { recursive: true, force: true });
    }
  });
}
