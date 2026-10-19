import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export const alias = {
  $lib: here("../src/lib"),
  "$app/state": here("./state.svelte.js"),
  "$app/stores": here("./stores.js"),
  "$app/navigation": here("./navigation.js"),
};
export default defineConfig({
  root: here("./"),
  base: "./",
  publicDir: false,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "dr-jobs-fixture-closure",
      generateBundle() {
        this.emitFile({
          type: "asset",
          fileName: "assets/SchibstedGrotesk-LICENSE.txt",
          source: readFileSync(here("../preview/fonts/OFL.txt"), "utf8"),
        });
        const modules = [...this.getModuleIds()];
        if (
          !modules.includes(
            here("../src/routes/public/app/dr-jobs/+page.svelte"),
          )
        )
          this.error("The preview must render the production DR jobs page");
        for (const id of modules) {
          if (
            /src\/lib\/(server|private)\/|src\/routes\/(private|friends)\/|\+page\.server\./.test(
              id,
            )
          )
            this.error(
              `Server or non-public code entered the DR jobs preview: ${id}`,
            );
        }
      },
    },
  ],
  resolve: { conditions: ["browser"], alias },
  build: { target: "es2022", outDir: here("../dr-jobs-fixture-preview") },
});
