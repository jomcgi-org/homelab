// Tests for archive.mjs. Run: node --test archive.test.mjs
import assert from "node:assert/strict";
import { test } from "node:test";
import { gzipSync } from "node:zlib";
import { ArchiveError, LIMITS, readPreview, readTar } from "./archive.mjs";
import { tarGz } from "./tar-fixture.mjs";

const site = (extra = []) =>
  tarGz([
    { path: "./", type: "5" },
    { path: "./index.html", data: "<!doctype html>" },
    { path: "./_headers", data: "/*\n  X-Content-Type-Options: nosniff\n" },
    { path: "./assets/app-abc.js", data: "console.log(1)" },
    { path: "./data/factory/manifest.json", data: "{}" },
    ...extra,
  ]);

const refuses = (archive, pattern, limits) =>
  assert.throws(
    () => readPreview(archive, limits),
    (error) => {
      assert.ok(error instanceof ArchiveError, error);
      assert.match(error.message, pattern);
      return true;
    },
  );

test("a static site passes, and _headers gains noindex for every path", () => {
  const files = readPreview(site());
  assert.deepEqual([...files.keys()].sort(), [
    "_headers",
    "assets/app-abc.js",
    "data/factory/manifest.json",
    "index.html",
  ]);
  const headers = files.get("_headers").toString();
  assert.match(headers, /X-Content-Type-Options: nosniff/);
  assert.match(headers, /\n\/\*\n {2}X-Robots-Tag: noindex\n$/);
});

test("a site with no _headers still gets noindex", () => {
  const files = readPreview(tarGz([{ path: "index.html", data: "x" }]));
  assert.match(files.get("_headers").toString(), /X-Robots-Tag: noindex/);
});

test("Worker scripts, routes and Functions are refused", () => {
  refuses(
    site([{ path: "./_worker.js", data: "export default {}" }]),
    /Worker code/,
  );
  refuses(site([{ path: "./_worker.js/index.js", data: "x" }]), /Worker code/);
  refuses(site([{ path: "./assets/_worker.js", data: "x" }]), /Worker code/);
  refuses(site([{ path: "./_routes.json", data: "{}" }]), /Worker code/);
  refuses(site([{ path: "./functions/api.js", data: "x" }]), /Functions/);
});

test("hidden paths, traversal and absolute paths are refused", () => {
  refuses(site([{ path: "./.vite/manifest.json", data: "{}" }]), /hidden/);
  refuses(
    site([{ path: "./.wrangler/deploy/config.json", data: "{}" }]),
    /hidden/,
  );
  refuses(site([{ path: "./assets/../../etc.txt", data: "x" }]), /hidden/);
  refuses(site([{ path: "/etc/passwd.txt", data: "x" }]), /refused path/);
  refuses(site([{ path: "a\\b.txt", data: "x" }]), /refused path/);
});

test("links, devices and duplicate files are refused", () => {
  refuses(
    site([{ path: "./link.html", type: "2", linkname: "/etc/passwd" }]),
    /regular files/,
  );
  refuses(
    site([{ path: "./hard.html", type: "1", linkname: "index.html" }]),
    /regular files/,
  );
  refuses(site([{ path: "./fifo.html", type: "6" }]), /regular files/);
  refuses(site([{ path: "index.html", data: "again" }]), /twice/);
});

test("file types outside the static list are refused", () => {
  refuses(
    site([{ path: "./server.mjs", data: "x" }]),
    /\.mjs is not a static file type/,
  );
  refuses(site([{ path: "./run.sh", data: "x" }]), /\.sh is not/);
  refuses(site([{ path: "./wrangler.jsonc", data: "{}" }]), /\.jsonc is not/);
  refuses(site([{ path: "./README", data: "x" }]), /\(none\)/);
  refuses(
    site([{ path: "./docs/_redirects", data: "/ /x 302" }]),
    /only allowed at the root/,
  );
});

test("_headers may not detach X-Robots-Tag", () => {
  refuses(
    tarGz([
      { path: "index.html", data: "x" },
      { path: "_headers", data: "/*\n  ! X-Robots-Tag\n" },
    ]),
    /X-Robots-Tag/,
  );
});

test("oversize files, totals and file counts are refused", () => {
  const small = { ...LIMITS, fileBytes: 10, totalBytes: 40, files: 3 };
  refuses(
    tarGz([
      { path: "index.html", data: "x" },
      { path: "big.txt", data: "x".repeat(11) },
    ]),
    /big\.txt: larger than 10/,
    small,
  );
  refuses(
    tarGz([
      { path: "index.html", data: "x" },
      { path: "a.txt", data: "x".repeat(10) },
      { path: "b.txt", data: "x".repeat(10) },
    ]),
    /more than 2 files/,
    { ...small, files: 2 },
  );
  // A gzip bomb fails while it is being inflated.
  refuses(
    gzipSync(Buffer.alloc(4 * 1024 * 1024)),
    /larger than 40 bytes/,
    small,
  );
});

test("the body must be a gzip archive with an index.html", () => {
  refuses(Buffer.from("not gzip"), /not a gzip archive/);
  refuses(tarGz([{ path: "page.html", data: "x" }]), /no index\.html/);
});

test("readTar takes long names from pax headers", () => {
  const long = `assets/${"a".repeat(120)}.js`;
  // A record's length counts its own digits.
  const record = (() => {
    const tail = ` path=${long}\n`;
    let length = tail.length + 1;
    while (`${length}${tail}`.length !== length) length += 1;
    return `${length}${tail}`;
  })();
  const entries = readTar(
    tarGz(
      [
        { path: "PaxHeader", type: "x", data: record },
        { path: "short", data: "x" },
      ],
      {
        gzip: false,
      },
    ),
  );
  assert.deepEqual(
    entries.map((e) => e.path),
    [long],
  );
});
