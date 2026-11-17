// HOME-224: check the emitted shell, its routing file and publication boundary.
// Usage after pnpm build: node factory-built.test.mjs dist
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { Window } from "happy-dom";

const dist = process.argv[2];
assert.ok(dist, "usage: node factory-built.test.mjs <dist directory>");

test("the Factory shell is pre-rendered with unavailable states and local assets", () => {
  const window = new Window({
    settings: {
      disableJavaScriptEvaluation: true,
      disableJavaScriptFileLoading: true,
      disableCSSFileLoading: true,
    },
  });
  try {
    const html = readFileSync(join(dist, "factory/index.html"), "utf8");
    assert.ok(!html.includes("<!--ssr-->"));
    window.document.write(html);
    const doc = window.document;
    assert.equal(doc.querySelector("h1")?.textContent, "Factory story");
    for (const label of [
      "Story",
      "Goals",
      "Statistics",
      "Work in progress",
      "Recently shipped",
    ]) {
      assert.ok(
        doc
          .querySelector("main")
          ?.textContent.includes(`${label} not available.`),
      );
    }
    assert.equal(doc.querySelectorAll("[style], style").length, 0);
    assert.equal(
      doc.querySelector("link[rel='canonical']")?.getAttribute("href"),
      "https://jomcgi.dev/factory",
    );
    for (const node of doc.querySelectorAll(
      "script[src], link[rel='stylesheet'], link[rel='modulepreload']",
    )) {
      const asset = node.getAttribute("src") ?? node.getAttribute("href");
      assert.match(asset, /^\/assets\//);
      assert.ok(existsSync(join(dist, asset.slice(1))), asset);
    }
  } finally {
    window.happyDOM.abort();
  }
});

test("the built rewrite serves descendants through the same shell", () => {
  assert.equal(
    readFileSync(join(dist, "_redirects"), "utf8").trim(),
    "/factory /factory/ 200\n/factory/* /factory/ 200",
  );
  assert.match(
    readFileSync(join(dist, "_headers"), "utf8"),
    /style-src 'self'/,
  );
});

test("the production output carries no invented snapshots", () => {
  assert.ok(!existsSync(join(dist, "data/factory")));
  const walk = (dir) =>
    readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
      entry.isDirectory()
        ? walk(join(dir, entry.name))
        : [join(dir, entry.name)],
    );
  for (const file of walk(dist).filter((file) =>
    /\.(?:html|js|json)$/.test(file),
  )) {
    assert.doesNotMatch(
      readFileSync(file, "utf8"),
      /Fixture edition|fictional reading list|demo-build|DEMO-104/,
    );
  }
});
