import { resolve } from "node:path";
import { defineConfig } from "vite";

// Every page is its own HTML entry, so each route ships only its own chunks.
// Hashed files go under /assets/, which public/_headers caches forever.
//
// Under Bazel the sources are symlinks into the sandbox. Absolute entries from
// the working directory keep Rollup's page names inside the build root.
const page = (file: string) => resolve(process.cwd(), file);

export default defineConfig({
  base: "/",
  esbuild: { jsx: "automatic" },
  build: {
    outDir: "dist",
    target: "es2022",
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    rollupOptions: {
      input: {
        index: page("index.html"),
        "404": page("404.html"),
        cv: page("cv.html"),
        trips: page("trips.html"),
        trip: page("trip.html"),
        "trip-day": page("trip-day.html"),
        "trip-timeline": page("trip-timeline.html"),
      },
      onwarn(warning, warn) {
        if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning);
      },
    },
  },
});
