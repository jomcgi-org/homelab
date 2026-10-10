import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  root: here("./"),
  base: "./",
  publicDir: false,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "factory-fixture-only-module-graph",
      generateBundle() {
        for (const id of this.getModuleIds()) {
          if (
            /src\/lib\/(server|private)\/|src\/routes\/(private|friends)\/|\+page\.server\.|posts-manifest\.json/.test(
              id,
            )
          ) {
            this.error(
              `Server module or non-fixture data entered preview: ${id}`,
            );
          }
        }
        this.emitFile({
          type: "asset",
          fileName: "assets/SchibstedGrotesk-LICENSE.txt",
          source: readFileSync(here("./fonts/OFL.txt"), "utf8"),
        });
        this.emitFile({
          type: "asset",
          fileName: "build.json",
          source: JSON.stringify({
            commit: process.env.FACTORY_PREVIEW_SHA ?? "local-unpublished",
            scope: "synthetic factory fixtures only",
          }),
        });
      },
    },
  ],
  resolve: {
    conditions: ["browser"],
    alias: [
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
    target: "es2022",
    outDir: here("../factory-fixture-preview"),
    emptyOutDir: true,
    sourcemap: false,
  },
});
