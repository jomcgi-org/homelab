import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export const aliases = [
  { find: "$lib", replacement: here("../src/lib") },
  ...["$app/stores", "$app/navigation", "$app/forms"].map((find) => ({
    find,
    replacement: here("./navigation.js"),
  })),
];
export default defineConfig({
  root: here("./"),
  publicDir: false,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "synthetic-tools-only",
      generateBundle() {
        for (const id of this.getModuleIds()) {
          if (
            /\+(?:page\.server|server)\.|src\/lib\/server\/|src\/routes\/friends\//.test(
              id,
            )
          )
            this.error(`Server source entered fixture: ${id}`);
        }
        this.emitFile({
          type: "asset",
          fileName: "build.json",
          source: JSON.stringify({
            head: process.env.FACTORY_PREVIEW_SHA ?? "unpublished",
            synthetic: true,
          }),
        });
      },
    },
  ],
  resolve: { conditions: ["browser"], alias: aliases },
  build: {
    target: "es2022",
    outDir: process.env.TOOLS_PREVIEW_OUT ?? here("../tools-fixture-preview"),
    emptyOutDir: true,
  },
});
