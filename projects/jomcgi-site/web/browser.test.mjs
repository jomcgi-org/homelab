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
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
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
          await page.goto(`${origin}/${entry}`, { waitUntil: "networkidle0" });
          const label = `${entry} ${scheme} ${width}`;
          assert.deepEqual(errors, [], label);
          const result = await page.evaluate(() => ({
            violations: window.cspViolations,
            overflow: document.documentElement.scrollWidth > innerWidth + 1,
          }));
          assert.deepEqual(result.violations, [], label);
          assert(!result.overflow, `${label}: horizontal page overflow`);
          await page.addScriptTag({ url: `${origin}/_axe.js` });
          const violations = await page.evaluate(async () =>
            (await window.axe.run()).violations.map(({ id, nodes }) => ({
              id,
              nodes: nodes.map(({ target }) => target),
            })),
          );
          assert.deepEqual(violations, [], label);
          if (entry === "homelab.html") {
            // Each in-page link lands on a section that exists. The stale
            // notice depends on the clock, so homelab.test.tsx covers it with
            // a fake one.
            const page_ = await page.evaluate(() => ({
              missing: [...document.querySelectorAll(".homelab-index a")]
                .map((link) => link.getAttribute("href").slice(1))
                .filter((id) => !document.getElementById(id)),
            }));
            assert.deepEqual(page_.missing, [], `${label}: index anchors`);
          }
          console.log(`PASS ${label}: axe, contrast, CSP, hydration`);
        }
      }
    }
    assert.deepEqual(errors, []);
  },
);
