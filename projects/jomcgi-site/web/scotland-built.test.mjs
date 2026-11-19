// HOME-207 acceptance on the actual build output, with page scripts disabled.
// Usage after pnpm build: node scotland-built.test.mjs dist
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import axe from "axe-core";
import { Window } from "happy-dom";
import { measure, overBudget } from "./budget.mjs";
import { evaluateHub, readHub } from "./hub-fixture.mjs";

const dist = process.argv[2];
assert.ok(dist, "usage: node scotland-built.test.mjs <dist directory>");
const hub = evaluateHub(
  [
    "lib/public/seo.js",
    "routes/public/scotland/scotland-data.js",
    "routes/public/scotland/policy-data.js",
  ],
  "({ PUBLIC_BASE, facts, setups, sources, policyLinks, privacy, terms, support, UPDATED })",
);
const migratedURL = (value) =>
  value
    .replaceAll(
      "https://jomcgi.dev/scotland/mcp",
      "https://scotland-mcp.jomcgi.dev/mcp",
    )
    .replaceAll(
      "https://jomcgi.dev/scotland/v1/",
      "https://scotland-mcp.jomcgi.dev/v1/",
    );
const normalize = (text) => text.replace(/\s+/g, " ").trim();
const literal = (source, name) => {
  const match = source.match(new RegExp(`${name}="([^"]+)"`));
  assert.ok(match, `hub source must declare ${name}`);
  return match[1];
};
const pages = ["scotland", "slop"].map((slug) => {
  const source = readHub(`routes/public/${slug}/+page.svelte`);
  return {
    slug,
    title: literal(source, "title"),
    description: literal(source, "description"),
    text: [...source.matchAll(/<(p|h[1-3])\b[^>]*>([^<{]+)<\/\1>/g)].map(
      (match) => normalize(match[2]),
    ),
  };
});
for (const policy of [hub.privacy, hub.terms, hub.support]) {
  pages.push({
    slug: `scotland/${policy.slug}`,
    title: `${policy.title} · Scotland connector · jomcgi.dev`,
    description: policy.description,
    text: [
      policy.title,
      policy.lead,
      `updated ${hub.UPDATED}`,
      ...policy.sections.flatMap((section) => [
        section.heading,
        ...section.paras,
      ]),
    ],
    links: policy.sections.flatMap((section) => section.rows ?? []),
  });
}

for (const page of pages) {
  test(`/${page.slug}: built HTML contains the hub content and metadata before scripts run`, async () => {
    const window = new Window({
      settings: {
        disableJavaScriptEvaluation: true,
        disableJavaScriptFileLoading: true,
        disableCSSFileLoading: true,
      },
    });
    try {
      const html = readFileSync(join(dist, `${page.slug}.html`), "utf8");
      assert.ok(
        !html.includes("<!--ssr-->"),
        "build must replace the prerender marker",
      );
      window.document.write(html);
      const doc = window.document;
      const root = doc.querySelector("#root");
      assert.ok(
        root?.querySelector("main"),
        "the built root must contain the page",
      );
      assert.equal(root.querySelectorAll("h1").length, 1);
      const content = normalize(root.textContent);
      for (const text of page.text)
        assert.ok(content.includes(normalize(text)), text);
      for (const link of page.links ?? []) {
        assert.ok(
          [...root.querySelectorAll("a")].some(
            (a) =>
              a.getAttribute("href") === migratedURL(link.href) &&
              a.textContent === link.v,
          ),
          link.href,
        );
      }
      if (page.slug === "scotland") {
        for (const fact of hub.facts)
          assert.ok(content.includes(migratedURL(fact.v)), fact.k);
        for (const setup of hub.setups) {
          assert.ok(content.includes(setup.how), setup.label);
          if (setup.code)
            assert.ok(
              [...root.querySelectorAll("pre")].some(
                (pre) => pre.textContent === migratedURL(setup.code),
              ),
              setup.id,
            );
        }
        for (const source of hub.sources) {
          for (const value of Object.values(source))
            assert.ok(content.includes(value), value);
        }
        for (const link of hub.policyLinks)
          assert.ok(root.querySelector(`a[href="${link.href}"]`), link.href);
      }

      assert.equal(doc.title, page.title);
      const canonical = `${hub.PUBLIC_BASE}/${page.slug}`;
      assert.equal(
        doc.querySelector('link[rel="canonical"]')?.getAttribute("href"),
        canonical,
      );
      for (const [key, expected] of Object.entries({
        description: page.description,
        "og:type": "website",
        "og:site_name": "Joe McGinley",
        "og:title": page.title,
        "og:description": page.description,
        "og:url": canonical,
        "twitter:card": "summary",
        "twitter:title": page.title,
        "twitter:description": page.description,
      })) {
        assert.equal(
          doc
            .querySelector(`meta[name="${key}"], meta[property="${key}"]`)
            ?.getAttribute("content"),
          expected,
          key,
        );
      }
      if (page.slug === "slop")
        assert.equal(
          doc.querySelector('meta[name="robots"]')?.getAttribute("content"),
          "noindex, nofollow",
        );

      assert.equal(
        doc.querySelectorAll("style, [style]").length,
        0,
        "no inline styles in built HTML",
      );
      // Structural axe rules on the deployed markup. happy-dom has no layout;
      // palette contrast is checked separately, and browser contrast on deploy.
      const accessibility = await axe.run(root, {
        rules: { "color-contrast": { enabled: false } },
      });
      assert.deepEqual(
        accessibility.violations.map((violation) => ({
          id: violation.id,
          nodes: violation.nodes.map((node) => node.target),
        })),
        [],
        "the built page must have no structural axe violations",
      );
      const scripts = [...doc.querySelectorAll("script:not([data-cf-beacon])")];
      assert.equal(scripts.length, 1, "one external hydration entry");
      assert.equal(scripts[0].getAttribute("type"), "module");
      const assets = [
        ...scripts.map((script) => script.getAttribute("src")),
        ...[
          ...doc.querySelectorAll(
            'link[rel="stylesheet"], link[rel="modulepreload"]',
          ),
        ].map((link) => link.getAttribute("href")),
      ];
      assert.ok(assets.length > 1, "built CSS must accompany the page");
      for (const asset of assets) {
        assert.match(asset ?? "", /^\/assets\//);
        assert.ok(
          existsSync(join(dist, asset.slice(1))),
          `missing asset ${asset}`,
        );
      }
    } finally {
      await window.happyDOM.close();
    }
  });
}

test("all six exact edge routes have files in the deployment output", () => {
  const routes = JSON.parse(
    readFileSync(new URL("../edge/routes.json", import.meta.url), "utf8"),
  );
  const expected = [
    ...pages.map((page) => `jomcgi.dev/${page.slug}`),
    "jomcgi.dev/scotland/marketplace.json",
  ].sort();
  // The legacy connector paths end in `*`; the next test covers them.
  const owned = routes.patterns
    .filter((pattern) => /^jomcgi\.dev\/(scotland|slop)/.test(pattern))
    .filter((pattern) => !pattern.endsWith("*"))
    .sort();
  assert.deepEqual(owned, expected);
  for (const pattern of expected) {
    const path = pattern.slice("jomcgi.dev/".length);
    assert.ok(
      existsSync(join(dist, path.endsWith(".json") ? path : `${path}.html`)),
      pattern,
    );
    assert.ok(routes.exactBecause[pattern], pattern);
  }
  for (const path of ["scotland/mcp", "scotland/v1", "slop/factory"]) {
    assert.ok(
      !existsSync(join(dist, path)) && !existsSync(join(dist, `${path}.html`)),
      `${path} must not be generated by this migration`,
    );
  }
});

test("the build sends the legacy connector URLs to scotland-mcp.jomcgi.dev", () => {
  // HOME-475. A 308 keeps the method and body, so an MCP POST follows it.
  const redirects = readFileSync(join(dist, "_redirects"), "utf8").split("\n");
  for (const rule of [
    "/scotland/mcp https://scotland-mcp.jomcgi.dev/mcp 308",
    "/scotland/v1/* https://scotland-mcp.jomcgi.dev/v1/:splat 308",
  ])
    assert.ok(redirects.includes(rule), rule);
  // A rule reaches a visitor only if a Worker route covers its path.
  const routes = JSON.parse(
    readFileSync(new URL("../edge/routes.json", import.meta.url), "utf8"),
  );
  for (const path of ["/scotland/mcp", "/scotland/v1/plan"]) {
    const url = `jomcgi.dev${path}`;
    assert.ok(
      routes.patterns.some(
        (pattern) =>
          pattern.endsWith("*") && url.startsWith(pattern.slice(0, -1)),
      ),
      path,
    );
  }
});

test("the deployed drafts headers and each page's JS budget survive the build", () => {
  const headers = readFileSync(join(dist, "_headers"), "utf8");
  assert.match(headers, /\n\/slop\n\s+X-Robots-Tag: noindex, nofollow/);
  const entries = new Set(pages.map((page) => `${page.slug}.html`));
  const results = measure(dist).filter((result) => entries.has(result.page));
  assert.equal(results.length, entries.size);
  assert.deepEqual(overBudget(results), []);
});
