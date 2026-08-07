import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  resolve: {
    alias: {
      // SvelteKit's `$lib` alias, for tests that exercise modules importing
      // through it (the agents markdown wrapper does).
      $lib: fileURLToPath(new URL("./src/lib", import.meta.url)),
      // The bare node test env has no SvelteKit plugin, so the `$app/environment`
      // virtual module does not resolve when a `+page.server.js` load (and, via
      // cache-headers.js, `versionedEtag`) is imported directly. Point it at a
      // stub with a fixed `version` so page-ETag assertions stay deterministic.
      "$app/environment": fileURLToPath(
        new URL("./test/app-environment-stub.js", import.meta.url),
      ),
      // Same reason for `$env/dynamic/private` (used by lib/server/trips-img.js
      // and, transitively, the trips server loads). The real module is injected
      // by SvelteKit at build time; the stub exposes process.env so tests can
      // control IMGPROXY_KEY/SALT.
      "$env/dynamic/private": fileURLToPath(
        new URL("./test/env-dynamic-private-stub.js", import.meta.url),
      ),
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.js"],
  },
});
