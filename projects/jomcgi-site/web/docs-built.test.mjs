// HOME-199: check the actual HTML, index and routes against the hub's content.
// Run after pnpm build: node docs-built.test.mjs. Bazel: :docs_contract_test.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { Window } from "happy-dom";
import {
  buildPathIndex,
  getMeta,
  renderDoc,
} from "../../monolith/frontend/src/lib/server/docs.js";

const read = (path) => JSON.parse(readFileSync(path, "utf8"));
const manifest = read("docs-manifest.json");
const index = read("docs/_src/search-index.json");
const routes = read("../edge/routes.json");
const paths = buildPathIndex(manifest);
const window = new Window();
const parse = (html) =>
  new window.DOMParser().parseFromString(html, "text/html");

function checkMeta(doc, { title, description, path, type }) {
  const url = `https://jomcgi.dev${path}`;
  assert.equal(doc.title, title);
  assert.equal(
    doc.querySelector('link[rel="canonical"]')?.getAttribute("href"),
    url,
  );
  const expected = {
    description,
    "og:type": type,
    "og:site_name": "Joe McGinley",
    "og:title": title,
    "og:description": description,
    "og:url": url,
    "twitter:card": "summary",
    "twitter:title": title,
    "twitter:description": description,
  };
  const actual = Object.fromEntries(
    [...doc.querySelectorAll("meta[name], meta[property]")]
      .map((meta) => [
        meta.getAttribute("name") ?? meta.getAttribute("property"),
        meta.getAttribute("content"),
      ])
      .filter(([key]) => key in expected),
  );
  assert.deepEqual(actual, expected);
}

// Undo only the documented accessibility enhancements. All original prose,
// lists, tables, links, heading anchors and code must otherwise stay identical.
function originalBody(body) {
  assert.ok(body, "missing pre-rendered document body");
  const copy = body.cloneNode(true);
  copy
    .querySelectorAll(".docs-diagram, .docs-diagram-description")
    .forEach((node) => node.remove());
  copy
    .querySelectorAll("details > pre.doc-mermaid")
    .forEach((source) => source.parentElement.replaceWith(source));
  copy
    .querySelectorAll("[tabindex]")
    .forEach((node) => node.removeAttribute("tabindex"));
  return copy.innerHTML;
}

for (const entry of manifest) {
  test(`${entry.slug}: published body and metadata equal the hub's rendering`, () => {
    const doc = parse(readFileSync(`dist/docs/${entry.slug}.html`, "utf8"));
    const meta = getMeta(entry);
    checkMeta(doc, {
      ...meta,
      title: `${entry.title} · Docs · jomcgi.dev`,
      path: `/docs/${entry.slug}`,
      type: "article",
    });
    const rendered = renderDoc(
      {
        ...entry,
        content: entry.content.replace(/^(?:\r?\n)*#\s+[^\r\n]+(?:\r?\n)?/, ""),
      },
      paths,
      () => {
        throw new Error("static docs must supply repository images");
      },
    );
    assert.equal(
      originalBody(doc.getElementById("doc-body")),
      originalBody(parse(rendered.html).body),
    );
  });
}

test("overview retains the live hub's metadata", () => {
  // Values from the hub's /docs/+page.server.js and public/components/Seo.svelte.
  checkMeta(parse(readFileSync("dist/docs.html", "utf8")), {
    title: "Documentation · jomcgi.dev",
    description:
      "Current-state documentation for the public projects, rendered from the repository.",
    path: "/docs",
    type: "website",
  });
});

test("static search indexes every filtered document and no other document", () => {
  assert.deepEqual(
    index.map(({ slug }) => slug),
    manifest.map(({ slug }) => slug),
  );
  for (const [i, entry] of manifest.entries()) {
    assert.equal(index[i].title, entry.title);
    assert.ok(
      index[i].text.includes(entry.content.toLowerCase()),
      `${entry.slug}: full text missing`,
    );
  }
});

test("the static Worker owns the overview and every published doc, including queries", () => {
  assert.equal(routes.script, "jomcgi-site");
  for (const path of [
    "/docs",
    "/docs/",
    ...manifest.map(({ slug }) => `/docs/${slug}`),
  ]) {
    for (const suffix of ["", "?ref=docs"]) {
      const url = `jomcgi.dev${path}${suffix}`;
      assert.ok(
        routes.patterns.some((pattern) =>
          pattern.endsWith("*")
            ? url.startsWith(pattern.slice(0, -1))
            : url === pattern,
        ),
        `${url}: still on the hub`,
      );
    }
  }
});
