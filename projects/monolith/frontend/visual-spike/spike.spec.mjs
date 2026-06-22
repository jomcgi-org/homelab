import { expect, test } from "@playwright/test";

// ADR tooling/010 de-risking spike.
//
// The single question this answers: does the rules_playwright-vendored chromium
// launch and rasterize a page as a hermetic Bazel test action on the linux RBE
// executors, without a runtime `playwright install`? That is the ADR's one
// High-likelihood / High-impact risk (fonts, /dev/shm, the chromium sandbox).
//
// Scope is deliberately a fixed static page set via setContent, NOT a real
// public route booted through the app + mock seam. That isolates the substrate
// question (browser-under-Bazel) from the app-boot machinery, which is the
// build-out phase. One variable at a time.
//
// We assert a valid, non-trivial PNG rather than diffing a committed baseline:
// pixel-exact baselining needs the CI-only baseline-commit-back flow (the live
// tool's sentinel mechanism) and is phase two. A blank or failed render yields a
// tiny PNG; a real 1280x720 render of a colored card with text is many KB, so
// size + signature is a sufficient launch-and-render signal for the spike.
const FIXTURE = `<!doctype html>
<html><head><meta charset="utf-8"><style>
  html, body { margin: 0; padding: 0; }
  body { width: 1280px; height: 720px; background: #101418; font-family: sans-serif; }
  .card { margin: 80px; padding: 48px; background: #f4c542; color: #101418; border-radius: 16px; }
  h1 { font-size: 48px; margin: 0 0 16px; }
  p { font-size: 20px; margin: 0; }
</style></head><body>
  <div class="card">
    <h1>Hermetic visual regression spike</h1>
    <p>rules_playwright chromium under Bazel RBE</p>
  </div>
</body></html>`;

const PNG_SIGNATURE = "89504e470d0a1a0a";

test("vendored chromium launches and renders under Bazel", async ({ page }) => {
  await page.setContent(FIXTURE, { waitUntil: "load" });
  const buf = await page.screenshot({ type: "png" });

  // Valid PNG header: rules out a truncated or non-image artifact.
  expect(buf.subarray(0, 8).toString("hex")).toBe(PNG_SIGNATURE);
  // A real render of the card + text is many KB; a blank/failed paint is tiny.
  // This is the launch + rasterize signal (fonts actually drew).
  expect(buf.length).toBeGreaterThan(5000);
});
