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
          const source = id.split("?")[0];
          const privateModule =
            /src\/lib\/private\/|src\/routes\/private\//.test(id);
          const allowedPrivate =
            /src\/lib\/private\/dashboard-theme\.css$|src\/routes\/private\/\+layout\.svelte$|src\/routes\/private\/factory\/factory-view\.js$|src\/routes\/private\/factory\/execution\/[^?]+\.(svelte|js|css)$/.test(
              source,
            );
          if (
            /src\/lib\/server\/|src\/routes\/friends\/|\+(?:page\.server|server)\.|posts-manifest\.json/.test(
              id,
            ) ||
            (privateModule && !allowedPrivate)
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
      { find: "$app/stores", replacement: here("./execution-stores.js") },
    ],
  },
  build: {
    target: "es2022",
    outDir: here("../factory-fixture-preview"),
    emptyOutDir: true,
    sourcemap: false,
  },
});
