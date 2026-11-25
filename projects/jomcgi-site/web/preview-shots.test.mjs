// Tests preview-shots.mjs: arguments, the local server and the exit codes
// on a small invented site in the pinned Chromium. CI target
// :preview_shots_test.
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync } from "node:fs";
import { rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  DEFAULT_PAGES,
  indexMarkdown,
  parseArgs,
  siteServer,
} from "./preview-shots.mjs";

const script = join(
  dirname(fileURLToPath(import.meta.url)),
  "preview-shots.mjs",
);
const root = mkdtempSync(join(process.env.TEST_TMPDIR ?? tmpdir(), "shots-"));
test.after(() => rmSync(root, { recursive: true, force: true }));
const dist = join(root, "dist");
const fixtures = join(root, "fixtures");
const object = `objects/${"a".repeat(64)}.json`;
const files = {
  "index.html": "<!doctype html><title>Home</title><p>home</p>",
  "404.html": "<!doctype html><title>Not found</title>",
  "blog.html": "<!doctype html><title>Blog</title>",
  "factory/index.html": "<!doctype html><title>Factory</title>",
  ".vite/manifest.json": "{}",
  "throws.html":
    "<!doctype html><title>Throws</title><script>console.error('broken')</script>",
  "asset.html":
    "<!doctype html><title>Asset</title><script src='/assets/gone.js'></script>",
  "data.html":
    "<!doctype html><title>Data</title><script>fetch('/data/stars/latest.json')</script>",
  "fixture.html":
    "<!doctype html><title>Fixture</title><script>fetch('/data/factory/objects/missing.json')</script>",
  _redirects:
    "/factory /factory/ 200\n/factory/* /factory/ 200\n/old /blog 301\n",
  _headers: "/*\n  X-Policy: site\n\n/stars\n  ! X-Policy\n  X-Policy: map\n",
};
for (const [name, body] of Object.entries(files)) {
  mkdirSync(dirname(join(dist, name)), { recursive: true });
  writeFileSync(join(dist, name), body);
}
mkdirSync(join(fixtures, "objects"), { recursive: true });
writeFileSync(join(fixtures, "manifest.json"), '{"version":1}');
writeFileSync(join(fixtures, object), '{"kind":"story"}');

test("arguments", () => {
  const base = ["--dist", "d", "--fixtures", "f", "--out", "o"];
  assert.deepEqual(parseArgs(base).pages, DEFAULT_PAGES);
  assert.deepEqual(parseArgs([...base, "/blog", "/factory?x=1"]).pages, [
    "/blog",
    "/factory?x=1",
  ]);
  for (const bad of [
    [...base, "blog"],
    [...base, "//evil.example/"],
    [...base, "https://evil.example/"],
    [...base, "/a/../b"],
    [...base, "/a b"],
    [...base, "--width", "1"],
    ["--dist"],
    ["--dist", "d", "--out", "o"],
  ])
    assert.throws(() => parseArgs(bad), /page must|unknown|needs|required/);
});

test("server", async () => {
  const server = siteServer(dist, fixtures);
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const get = (path) => fetch(origin + path, { redirect: "manual" });
  try {
    let response = await get("/");
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("x-policy"), "site");
    assert.match(await (await get("/blog")).text(), /Blog/);
    assert.match(await (await get("/factory/goals/x")).text(), /Factory/);
    response = await get("/stars");
    assert.equal(response.headers.get("x-policy"), "map");
    response = await get("/old");
    assert.equal(response.status, 301);
    assert.equal(response.headers.get("location"), "/blog");
    response = await get("/nope");
    assert.equal(response.status, 404);
    assert.match(await response.text(), /Not found/);
    assert.equal((await get("/.vite/manifest.json")).status, 404);
    // An encoded slash keeps ".." in the path that reaches the server.
    assert.equal((await get("/..%2ffixtures%2fmanifest.json")).status, 404);
    assert.equal((await get("/x/..%2f..%2fpackage.json")).status, 404);
    assert.equal(
      await (await get("/data/factory/manifest.json")).text(),
      '{"version":1}',
    );
    assert.equal((await get(`/data/factory/${object}`)).status, 200);
    assert.equal((await get("/data/factory/rehash.mjs")).status, 404);
  } finally {
    await new Promise((done) => server.close(done));
  }
});

test("index lists pages and screenshots", () => {
  const md = indexMarkdown([
    {
      page: "/",
      width: 390,
      scheme: "light",
      file: "home-phone-light.png",
      failures: [],
      notes: [],
    },
    {
      page: "/x|y",
      width: 390,
      scheme: "dark",
      file: "x-y-phone-dark.png",
      failures: ["HTTP 404: /x"],
      notes: [],
    },
  ]);
  assert.match(md, /Pages: 2\. Screenshots: 2\. Pages that failed: 1\./);
  assert.match(md, /\| `\/` \| ok \|/);
  assert.match(md, /\| `\/x\\\|y` \| FAIL \| HTTP 404: \/x \|/);
  assert.match(md, /\| `\/` \| 390 \| light \| home-phone-light\.png \|/);
});

const run = (out, ...pages) =>
  spawnSync(
    process.execPath,
    [script, "--dist", dist, "--fixtures", fixtures, "--out", out, ...pages],
    { encoding: "utf8" },
  );

test("clean pages exit 0 with four screenshots each", () => {
  const out = join(root, "ok");
  mkdirSync(out);
  writeFileSync(join(out, "keep.txt"), "not ours");
  writeFileSync(join(out, "stale.png"), "");
  const result = run(out, "/", "/factory/goals/x", "/data");
  assert.equal(result.status, 0, result.stdout + result.stderr);
  assert.deepEqual(readdirSync(out).sort(), [
    "data-desktop-dark.png",
    "data-desktop-light.png",
    "data-phone-dark.png",
    "data-phone-light.png",
    "factory-goals-x-desktop-dark.png",
    "factory-goals-x-desktop-light.png",
    "factory-goals-x-phone-dark.png",
    "factory-goals-x-phone-light.png",
    "home-desktop-dark.png",
    "home-desktop-light.png",
    "home-phone-dark.png",
    "home-phone-light.png",
    "index.md",
    "keep.txt",
  ]);
  const md = readFileSync(join(out, "index.md"), "utf8");
  assert.match(
    md,
    /\| `\/data` \| ok \| no local data: \/data\/stars\/latest\.json \|/,
  );
});

for (const [page, reason] of [
  ["/nope", "HTTP 404: /nope"],
  ["/throws", "console error: broken"],
  ["/asset", "HTTP 404: /assets/gone.js"],
  ["/fixture", "HTTP 404: /data/factory/objects/missing.json"],
])
  test(`${page} fails with exit 1`, () => {
    const out = join(root, page.slice(1));
    const result = run(out, "/", page);
    assert.equal(result.status, 1, result.stdout + result.stderr);
    const md = readFileSync(join(out, "index.md"), "utf8");
    assert.match(md, /Pages that failed: 1\./);
    assert.ok(md.includes(`| \`${page}\` | FAIL | ${reason}`), md);
  });

test("bad arguments exit 2 before any browser starts", () => {
  const result = run(join(root, "bad"), "blog");
  assert.equal(result.status, 2);
  assert.match(result.stderr, /page must be a site path/);
  const missing = spawnSync(
    process.execPath,
    [
      script,
      "--dist",
      join(root, "none"),
      "--fixtures",
      fixtures,
      "--out",
      join(root, "bad"),
    ],
    { encoding: "utf8" },
  );
  assert.equal(missing.status, 2);
  assert.match(missing.stderr, /no built site/);
});

for (const signal of ["SIGINT", "SIGTERM"])
  test(`${signal} stops the server and the browser`, async () => {
    const scratch = join(root, `signal-${signal}`);
    mkdirSync(scratch);
    const child = spawn(
      process.execPath,
      [
        script,
        "--dist",
        dist,
        "--fixtures",
        fixtures,
        "--out",
        join(scratch, "out"),
        "/",
        "/blog",
        "/factory",
      ],
      { env: { ...process.env, TMPDIR: scratch } },
    );
    let stdout = "";
    const exited = new Promise((done) =>
      child.on("exit", (code, killedBy) => done({ code, killedBy })),
    );
    // Signal while the browser is between screenshots.
    await new Promise((done, failed) => {
      child.stdout.on("data", (data) => {
        stdout += data;
        if (/^ok \/ /m.test(stdout)) done();
      });
      exited.then(() => failed(new Error(`exited early: ${stdout}`)));
    });
    const origin = stdout.match(/serving (http:\S+)/)[1];
    child.kill(signal);
    const { code } = await exited;
    assert.equal(code, signal === "SIGINT" ? 130 : 143, stdout);
    await assert.rejects(fetch(origin), "the server still answers");
    // Every process whose command line names the scratch directory.
    const left = readdirSync("/proc")
      .filter((pid) => /^\d+$/.test(pid))
      .map((pid) => {
        try {
          return readFileSync(`/proc/${pid}/cmdline`, "utf8");
        } catch {
          return "";
        }
      })
      .filter((line) => line.includes(scratch));
    assert.deepEqual(left, [], "a browser process is left");
    assert.deepEqual(
      readdirSync(scratch).filter((name) => name.startsWith("preview-shots-")),
      [],
      "the browser scratch directory is left",
    );
  });
