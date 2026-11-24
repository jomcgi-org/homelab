// The /homelab overview replaced the rendered Docs section. Check the emitted
// page, that no docs page is built, and that every old /docs URL redirects.
// Usage after pnpm build: node homelab-built.test.mjs dist
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";
import { Window } from "happy-dom";

const dist = process.argv[2];
assert.ok(dist, "usage: node homelab-built.test.mjs <dist directory>");
const here = dirname(fileURLToPath(import.meta.url));
const read = (path) => readFileSync(join(dist, path), "utf8");

test("the homelab page is pre-rendered with its sections and metadata", () => {
  const window = new Window({
    settings: {
      disableJavaScriptEvaluation: true,
      disableJavaScriptFileLoading: true,
      disableCSSFileLoading: true,
    },
  });
  try {
    const html = read("homelab.html");
    assert.ok(!html.includes("<!--ssr-->"));
    window.document.write(html);
    const doc = window.document;
    assert.equal(doc.querySelectorAll("h1").length, 1);
    assert.equal(doc.querySelector("h1")?.textContent, "The homelab");
    assert.deepEqual(
      [...doc.querySelectorAll("main h2")].map((h) => h.textContent),
      ["How it fits", "Containing a threat", "Method", "Projects"],
    );
    assert.equal(
      doc.querySelector("link[rel='canonical']")?.getAttribute("href"),
      "https://jomcgi.dev/homelab",
    );
    assert.ok(doc.querySelector("meta[name='description']")?.content);
    // The built page cannot know its age, so it never claims to be stale.
    assert.ok(!doc.querySelector(".homelab-stale"));
    assert.ok(doc.querySelector("time[datetime]"));
    assert.equal(doc.querySelectorAll("[style], style").length, 0);
  } finally {
    window.happyDOM.abort();
  }
});

test("no docs page is built and the sitemap lists the homelab page", () => {
  assert.ok(!existsSync(join(dist, "docs.html")));
  assert.ok(!existsSync(join(dist, "docs")));
  const sitemap = read("sitemap.xml");
  assert.ok(sitemap.includes("<loc>https://jomcgi.dev/homelab</loc>"));
  assert.ok(!sitemap.includes("/docs"));
  assert.ok(read("llms.txt").includes("/homelab"));
});

test("every old docs URL redirects to the homelab page, and the edge routes both", () => {
  const redirects = readFileSync(join(here, "public/_redirects"), "utf8").split(
    "\n",
  );
  assert.ok(redirects.includes("/docs /homelab 301"));
  assert.ok(redirects.includes("/docs/* /homelab 301"));
  const routes = JSON.parse(
    readFileSync(join(here, "../edge/routes.json"), "utf8"),
  ).patterns;
  // /docs* stays routed so the Worker can answer old links with the redirect.
  assert.ok(routes.includes("jomcgi.dev/docs*"));
  assert.ok(routes.includes("jomcgi.dev/homelab*"));
});
