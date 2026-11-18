import { readFileSync } from "node:fs";
import { homeDefines } from "./site-inputs.mjs";
import { resolve } from "node:path";
import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { blogContent, manifest } from "./blog-content.mjs";
import { factoryFixtures } from "./factory-fixtures";

// Every page is its own HTML entry, so each route ships only its own chunks.
// Hashed files go under /assets/, which public/_headers caches forever.
//
// Under Bazel the sources are symlinks into the sandbox. Absolute entries from
// the working directory keep Rollup's page names inside the build root.
const page = (file: string) => resolve(process.cwd(), file);

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
      input: {
        index: page("index.html"),
        factory: page("factory/index.html"),
        "404": page("404.html"),
        cv: page("cv.html"),
        scotland: page("scotland.html"),
        "scotland-privacy": page("scotland/privacy.html"),
        "scotland-terms": page("scotland/terms.html"),
        "scotland-support": page("scotland/support.html"),
        slop: page("slop.html"),
        blog: page("blog.html"),
        ...Object.fromEntries(
          manifest.map(({ slug }) => [
            `blog/${slug}`,
            page(`blog/${slug}.html`),
          ]),
        ),
        "llm-leaderboard": page("llm-leaderboard.html"),
        docs: page("docs.html"),
        ...Object.fromEntries(
          (
            JSON.parse(readFileSync("docs-manifest.json", "utf8")) as {
              slug: string;
            }[]
          ).map(({ slug }) => [`docs/${slug}`, page(`docs/${slug}.html`)]),
        ),
        trips: page("trips.html"),
        trip: page("trip.html"),
        "trip-day": page("trip-day.html"),
        "trip-timeline": page("trip-timeline.html"),
        engineering: page("engineering.html"),
        hikes: page("hikes.html"),
        stars: page("stars.html"),
      },
      onwarn(warning, warn) {
        if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning);
      },
    },
  },
});
