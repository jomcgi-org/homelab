import { defineConfig } from "@playwright/test";

// De-risking spike config (ADR tooling/010). Single worker, no retries: under a
// spike a flake must fail loudly, not be papered over. The browser binary comes
// from the rules_playwright-vendored repo via PLAYWRIGHT_BROWSERS_PATH (set by
// the BUILD target), NOT from a runtime `playwright install` and NOT from a
// system `channel` (no `channel: "chrome"` here, or it would look for a
// system-installed Chrome instead of the vendored chromium).
export default defineConfig({
  testDir: ".",
  testMatch: ["spike.spec.mjs"],
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    browserName: "chromium",
    headless: true,
    viewport: { width: 1280, height: 720 },
    launchOptions: {
      // Chromium's setuid sandbox needs user namespaces that the RBE execution
      // container typically does not grant; --disable-dev-shm-usage avoids the
      // small /dev/shm that crashes chromium under containerized CI. These are
      // exactly the runtime hazards ADR tooling/010 flagged as High/High.
      args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
    },
  },
});
