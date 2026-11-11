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
      name: "synthetic-private-review-boundary",
      generateBundle() {
        for (const moduleId of this.getModuleIds()) {
          const id = moduleId.split("?")[0];
          if (
            /src\/routes\/|src\/lib\/server\/|\$env\//.test(id) ||
            (/src\/lib\/private\//.test(id) &&
              !/src\/lib\/private\/components\/(ReviewCard|ModeToggle|UndoToast)\.svelte$/.test(
                id,
              ))
          )
            this.error(
              `Non-fixture module entered private review preview: ${id}`,
            );
        }
        this.emitFile({
          type: "asset",
          fileName: "assets/SchibstedGrotesk-LICENSE.txt",
          source: readFileSync(here("../preview/fonts/OFL.txt"), "utf8"),
        });
      },
    },
  ],
  resolve: { conditions: ["browser"] },
  build: {
    target: "es2022",
    outDir: here("../review-fixture-preview"),
    sourcemap: false,
  },
});
