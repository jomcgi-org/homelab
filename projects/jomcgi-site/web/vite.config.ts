import { homeDefines } from "./site-inputs.mjs";
import { resolve } from "node:path";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { blogContent, manifest } from "./blog-content.mjs";
import { factoryFixtures } from "./factory-fixtures";
import { built } from "./src/sections";

// Every page is its own HTML entry, so each route ships only its own chunks.
// Hashed files go under /assets/, which public/_headers caches forever.
//
// Under Bazel the sources are symlinks into the sandbox. Absolute entries from
// the working directory keep Rollup's page names inside the build root.
const page = (file: string) => resolve(process.cwd(), file);
// A section that is not live has no input, so none of its code ships.
const pages = (inputs: Record<string, string>) =>
  Object.fromEntries(
    Object.entries(inputs)
      .filter(([, file]) => built(file))
      .map(([name, file]) => [name, page(file)]),
  );

export default defineConfig({
  define: homeDefines,
  base: "/",
  resolve: { conditions: ["browser", "module", "import", "default"] },
  plugins: [blogContent(), svelte(), factoryFixtures()],
  esbuild: { jsx: "automatic" },
  optimizeDeps: { esbuildOptions: { target: "es2022" } },
  worker: {
    rollupOptions: {
      output: {
        // The CSP travels with the immutable worker response. Rotate its URL
        // when the policy changes, even when the worker bytes are identical.
        entryFileNames: ({ name }) =>
          name === "maplibre-gl-worker"
            ? "assets/[name]-csp2-[hash].js"
            : "assets/[name]-[hash].js",
      },
    },
  },
  build: {
    outDir: "dist",
    manifest: true,
    target: "es2022",
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    rollupOptions: {
      input: pages({
        index: "index.html",
        factory: "factory/index.html",
        "404": "404.html",
        cv: "cv.html",
        scotland: "scotland.html",
        "scotland-privacy": "scotland/privacy.html",
        "scotland-terms": "scotland/terms.html",
        "scotland-support": "scotland/support.html",
        slop: "slop.html",
        blog: "blog.html",
        ...Object.fromEntries(
          manifest.map(({ slug }) => [`blog/${slug}`, `blog/${slug}.html`]),
        ),
        "llm-leaderboard": "llm-leaderboard.html",
        homelab: "homelab.html",
        trips: "trips.html",
        trip: "trip.html",
        "trip-day": "trip-day.html",
        "trip-timeline": "trip-timeline.html",
        engineering: "engineering.html",
        hikes: "hikes.html",
        stars: "stars.html",
      }),
      onwarn(warning, warn) {
        if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning);
      },
    },
  },
});
