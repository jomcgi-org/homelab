// Acceptance against the published HTML and JS, with the real hosting CSP.
// Run after build: node browser.test.mjs. Bazel: :browser_test.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { htmlPages } from "./budget.mjs";
import { withSiteBrowser } from "./browser.mjs";

const require = createRequire(import.meta.url);
await withSiteBrowser(
  { assets: { "/_axe.js": require.resolve("axe-core/axe.min.js") } },
  async (page, origin) => {
    const accessibility = await page.createCDPSession();
    async function diagramAlternatives() {
      const { root } = await accessibility.send("DOM.getDocument");
      const { nodeIds } = await accessibility.send("DOM.querySelectorAll", {
        nodeId: root.nodeId,
        selector: ".docs-diagram svg",
      });
      return Promise.all(
        nodeIds.map(async (nodeId) => {
          const { nodes } = await accessibility.send(
            "Accessibility.getPartialAXTree",
            {
              nodeId,
              fetchRelatives: false,
            },
          );
          const image = nodes.find(
            (node) => !node.ignored && node.role?.value === "image",
          );
          assert(image, "Diagram must be exposed as an image");
          return image;
        }),
      );
    }
    const errors = [];
    let requests = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("request", (request) => requests.push(request.url()));
    await page.evaluateOnNewDocument(() => {
      window.cspViolations = [];
      addEventListener("securitypolicyviolation", (event) =>
        window.cspViolations.push(event.violatedDirective),
      );
    });
    for (const scheme of ["light", "dark"]) {
      await page.emulateMediaFeatures([
        { name: "prefers-color-scheme", value: scheme },
      ]);
      for (const width of [390, 1440]) {
        await page.setViewport({ width, height: 1000 });
        for (const entry of htmlPages("dist")) {
          requests = [];
          await page.goto(`${origin}/${entry}`, { waitUntil: "networkidle0" });
          const label = `${entry} ${scheme} ${width}`;
          assert.deepEqual(errors, [], label);
          assert(
            !requests.some((url) => url.includes("docs-search-index")),
            `${label}: eager search index`,
          );
          const result = await page.evaluate(() => ({
            violations: window.cspViolations,
            styles: document.querySelectorAll(
              ".docs-prose [style], .docs-prose style",
            ).length,
            sources: document.querySelectorAll("pre.doc-mermaid").length,
            diagrams: [...document.querySelectorAll(".docs-diagram svg")].map(
              (svg) => ({
                width: svg.getBoundingClientRect().width,
                height: svg.getBoundingClientRect().height,
                labels: svg.querySelectorAll("text").length,
                name: document.getElementById(
                  svg.getAttribute("aria-labelledby"),
                )?.textContent,
                description: document.getElementById(
                  svg.getAttribute("aria-describedby"),
                )?.textContent,
              }),
            ),
            overflowingLabels: [
              ...document.querySelectorAll(".docs-diagram .node"),
            ].flatMap((node) => {
              const shape = node
                .querySelector(":scope > rect.label-container")
                ?.getBoundingClientRect();
              const text = node.querySelector(".label text");
              if (!shape || !text) return [];
              const label = text.getBoundingClientRect();
              return label.left < shape.left - 1 ||
                label.right > shape.right + 1
                ? [text.textContent]
                : [];
            }),
            overflow: document.documentElement.scrollWidth > innerWidth + 1,
          }));
          assert.deepEqual(result.violations, [], label);
          assert.equal(result.styles, 0, label);
          assert.equal(result.diagrams.length, result.sources, label);
          assert.deepEqual(
            result.overflowingLabels,
            [],
            `${label}: diagram labels outside nodes`,
          );
          assert(!result.overflow, `${label}: horizontal page overflow`);
          for (const svg of result.diagrams)
            assert(
              svg.width > 0 &&
                svg.height > 0 &&
                svg.labels > 0 &&
                svg.name &&
                svg.description,
              `${label}: empty diagram`,
            );
          const alternatives = await diagramAlternatives();
          assert.equal(
            alternatives.length,
            result.diagrams.length,
            `${label}: exposed diagram images`,
          );
          for (const [index, svg] of result.diagrams.entries()) {
            const alternative = alternatives[index];
            assert.equal(
              alternative.name?.value,
              svg.name,
              `${label}: diagram name`,
            );
            assert.equal(
              alternative?.description?.value.replace(/\s+/g, " "),
              svg.description.replace(/\s+/g, " "),
              `${label}: full text equivalent in the accessibility tree`,
            );
          }
          await page.addScriptTag({ url: `${origin}/_axe.js` });
          const violations = await page.evaluate(async () =>
            (await window.axe.run()).violations.map(({ id, nodes }) => ({
              id,
              nodes: nodes.map(({ target }) => target),
            })),
          );
          assert.deepEqual(violations, [], label);
          if (entry === "docs.html") {
            await page.focus("#docs-search");
            await page.keyboard.type("sextant");
            await page.waitForSelector(".docs-results a");
            assert(
              requests.some((url) => url.includes("docs-search-index")),
              `${label}: missing intent-loaded index`,
            );
            const searchViolations = await page.evaluate(async () =>
              (await window.axe.run()).violations.map(({ id }) => id),
            );
            assert.deepEqual(
              searchViolations,
              [],
              `${label}: open search results`,
            );
            await page.keyboard.press("Escape");
            assert.equal(await page.$(".docs-results"), null);
          }
          console.log(
            `PASS ${label}: axe, contrast, CSP, hydration and diagrams`,
          );
        }
      }
    }
    // The source is an optional disclosure; the diagram works without JavaScript.
    await page.setJavaScriptEnabled(false);
    await page.goto(`${origin}/docs/sextant`);
    assert.equal(
      await page.$$eval(".docs-diagram svg", (svgs) => svgs.length),
      2,
    );
    const [generation, transitions] = await diagramAlternatives();
    assert.match(
      generation.description.value,
      /modelcache\.sextant\.yaml \(declarative spec\) points to sextant generate/,
    );
    assert.match(
      generation.description.value,
      /sextant generate points to 8 generated \.go files/,
    );
    assert.match(
      generation.description.value,
      /CI drift test regenerate \+ byte-compare points to 8 generated \.go files.*\(dashed\): fails on any diff, either direction/,
    );
    assert.match(
      transitions.description.value,
      /Pending transitions to Ready: CacheHit/,
    );
    assert.match(
      transitions.description.value,
      /Failed transitions to Pending: Retry \[retryable\]/,
    );
    // Readers can also open and navigate a normal text list without JavaScript.
    await page.click(".docs-diagram-description summary");
    assert(
      await page.$eval(".docs-diagram-description", (details) => details.open),
    );
    assert.match(
      await page.$eval(
        ".docs-diagram-description ul",
        (list) => list.innerText,
      ),
      /points to sextant generate/,
    );
    await page.setJavaScriptEnabled(true);
    await page.goto(`${origin}/docs`, { waitUntil: "networkidle0" });
    await page.keyboard.down("Control");
    await page.keyboard.press("k");
    await page.keyboard.up("Control");
    assert.equal(
      await page.evaluate(() => document.activeElement.id),
      "docs-search",
    );
    await page.keyboard.type("sextant");
    await page.waitForSelector(".docs-results a");
    const first = await page.$eval(".docs-results a", (a) => a.href);
    await Promise.all([
      page.waitForNavigation({ waitUntil: "networkidle0" }),
      page.keyboard.press("Enter"),
    ]);
    assert.equal(page.url(), first);
    // Arrow keys focus real links; native Enter opens the selected result.
    await page.focus("#docs-search");
    await page.type("#docs-search", "architecture");
    await page.waitForSelector(".docs-results a");
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("ArrowDown");
    const selected = await page.evaluate(() => document.activeElement.href);
    assert.equal(
      selected,
      await page.$$eval(".docs-results a", (links) => links[1].href),
    );
    await Promise.all([
      page.waitForNavigation({ waitUntil: "networkidle0" }),
      page.keyboard.press("Enter"),
    ]);
    assert.equal(page.url(), selected);
    assert.deepEqual(errors, []);
    console.log(
      "PASS static diagrams without JS, lazy search, Ctrl+K and Enter on default/selected results",
    );
  },
);
