import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: {
    alias: {
      // The bare node test env has no SvelteKit plugin, so the `$app/environment`
      // virtual module does not resolve when a `+page.server.js` load (and, via
      // cache-headers.js, `versionedEtag`) is imported directly. Point it at a
      // stub with a fixed `version` so page-ETag assertions stay deterministic.
      "$app/environment": fileURLToPath(
        new URL("./test/app-environment-stub.js", import.meta.url),
      ),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.js"],
  },
});
