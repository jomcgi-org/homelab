import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export const aliases = [
  { find: /^\$lib\/public\/components$/, replacement: here("./components.js") },
  { find: "$lib", replacement: here("../src/lib") },
  { find: "$app/stores", replacement: here("./state.js") },
  { find: "$app/navigation", replacement: here("./state.js") },
];
export default defineConfig({
  root: here("./"),
  base: "./",
  publicDir: false,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "synthetic-private-factory-only",
      generateBundle() {
        const sources = {};
        for (const id of this.getModuleIds()) {
          if (
            /src\/lib\/server\/|\+page\.server\.|posts-manifest\.json/.test(id)
          )
            this.error(`Non-fixture module: ${id}`);
          if (
            /src\/routes\/private\//.test(id) &&
            !/\/private\/(?:\+layout\.svelte|factory\/(?:FactorySheet\.svelte|\+page\.svelte|factory-view\.js|execution\/(?:run-history|run-lexicon|dag)\.js|escalations\/(?:\+page\.svelte|escalations-view\.js)|work-items\/\[id\]\/(?:\+page\.svelte|work-item-view\.js)))(?:\?|$)/.test(
              id,
            )
          )
            this.error(`Out-of-scope private module: ${id}`);
          if (id.startsWith(here("../src/")) && !id.includes("?"))
            sources[id.slice(here("../").length)] = createHash("sha256")
              .update(readFileSync(id))
              .digest("hex");
        }
        this.emitFile({
          type: "asset",
          fileName: "build.json",
          source: JSON.stringify(
            {
              commit: process.env.FACTORY_PREVIEW_SHA ?? "local-unpublished",
              scope:
                "synthetic private factory routes, real layouts and inherited legacy CSS",
              sources,
            },
            null,
            2,
          ),
        });
        this.emitFile({
          type: "asset",
          fileName: "assets/SchibstedGrotesk-LICENSE.txt",
          source: readFileSync(
            here("../factory-preview/fonts/OFL.txt"),
            "utf8",
          ),
        });
      },
    },
  ],
  resolve: { conditions: ["browser"], alias: aliases },
  build: {
    target: "es2022",
    outDir:
      process.env.PRIVATE_FACTORY_PREVIEW_OUT ??
      here("../private-factory-fixture-preview"),
    emptyOutDir: true,
  },
});
