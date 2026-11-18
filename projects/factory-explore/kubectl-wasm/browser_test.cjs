const assert = require("node:assert/strict");
const { execFile } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { promisify } = require("node:util");
const { gunzipSync } = require("node:zlib");

const [runtime, asset, ...args] = process.argv.slice(2);

async function runKubectl() {
  globalThis.fs = fs;
  require(path.resolve(runtime));
  const go = new Go();
  go.argv = ["kubectl", ...args];
  go.env = { HOME: process.env.TEST_TMPDIR || process.env.TMPDIR || "/tmp" };
  go.exit = process.exit;
  const { instance } = await WebAssembly.instantiate(
    gunzipSync(fs.readFileSync(asset)),
    go.importObject,
  );
  await go.run(instance);
}

async function test() {
  const run = (...command) =>
    promisify(execFile)(
      process.execPath,
      [__filename, runtime, asset, ...command],
      { argv0: "browser", timeout: 60000 },
    );
  assert.match(
    (await run("version", "--client")).stdout,
    /Client Version: v1\.37\.2\b/,
  );

  const listPath = "/api/v1/namespaces/default/pods";
  const responses = {
    "/api": { kind: "APIVersions", apiVersion: "v1", versions: ["v1"] },
    "/apis": { kind: "APIGroupList", apiVersion: "v1", groups: [] },
    "/api/v1": {
      kind: "APIResourceList",
      apiVersion: "v1",
      groupVersion: "v1",
      resources: [
        {
          name: "pods",
          singularName: "pod",
          namespaced: true,
          kind: "Pod",
          verbs: ["get", "list"],
        },
      ],
    },
    [listPath]: {
      kind: "Table",
      apiVersion: "meta.k8s.io/v1",
      columnDefinitions: [
        { name: "Name", type: "string", format: "name" },
        { name: "Status", type: "string" },
      ],
      rows: [
        {
          cells: ["browser-test", "Running"],
          object: {
            apiVersion: "v1",
            kind: "Pod",
            metadata: { name: "browser-test", namespace: "default" },
          },
        },
      ],
    },
  };
  const requests = [];
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, "http://localhost").pathname;
    requests.push([req.method, pathname]);
    res.writeHead(responses[pathname] ? 200 : 404, {
      "Content-Type": "application/json",
    });
    res.end(JSON.stringify(responses[pathname] || {}));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const { stdout } = await run(
      "--server",
      `http://127.0.0.1:${server.address().port}`,
      "get",
      "pods",
    );
    assert.match(stdout.trim(), /^NAME\s+STATUS\r?\nbrowser-test\s+Running$/);
    assert.ok(
      requests.some(
        ([method, pathname]) => method === "GET" && pathname === listPath,
      ),
    );
  } finally {
    server.close();
  }
}

(args.length ? runKubectl() : test()).catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
