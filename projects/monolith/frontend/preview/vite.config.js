import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  root: here("./"),
  // Relative URLs allow exactly these tested bytes at /pr/<number>/<sha>/.
  base: "./",
  publicDir: false,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "fixture-only-module-graph",
      generateBundle() {
        this.emitFile({
          type: "asset",
          fileName: "assets/SchibstedGrotesk-LICENSE.txt",
          source: readFileSync(here("./fonts/OFL.txt"), "utf8"),
        });
        for (const id of this.getModuleIds()) {
          if (
            /src\/lib\/(server|private)\/|src\/routes\/(private|friends)\/|posts-manifest\.json|posts\/(qwen|conformance)-replay\.json/.test(
              id,
            )
          ) {
            this.error(
              `Non-fixture data or server module entered preview: ${id}`,
            );
          }
        }
      },
    },
  ],
  resolve: {
    conditions: ["browser"],
    alias: [
      {
        find: /^\.\/qwen-replay\.json$/,
        replacement: here("./fixtures/qwen-replay.json"),
      },
      {
        find: "$lib/public/posts/ConformanceReplay.svelte",
        replacement: here("./UnsupportedScenario.svelte"),
      },
      {
        find: /^\$lib\/public\/components$/,
        replacement: here("./components.js"),
      },
      { find: "$lib", replacement: here("../src/lib") },
      { find: "$app/state", replacement: here("./state.svelte.js") },
      { find: "$app/navigation", replacement: here("./navigation.js") },
    ],
  },
  build: {
    // Match the production app target for the pinned Svelte/esbuild toolchain.
    target: "es2022",
    outDir: here("../fixture-preview"),
    emptyOutDir: true,
    sourcemap: false,
  },
});
