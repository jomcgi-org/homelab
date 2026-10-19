import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  root: here("./"),
  publicDir: false,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "trips-fixture-only",
      generateBundle() {
        this.emitFile({
          type: "asset",
          fileName: "assets/SchibstedGrotesk-LICENSE.txt",
          source: readFileSync(here("../preview/fonts/OFL.txt"), "utf8"),
        });
        for (const id of this.getModuleIds()) {
          if (
            /src\/lib\/(server|private)\/|src\/routes\/(private|friends)\//.test(
              id,
            )
          )
            this.error(`Non-fixture module entered trips preview: ${id}`);
        }
      },
    },
  ],
  resolve: {
    conditions: ["browser"],
    alias: {
      $lib: here("../src/lib"),
      "$app/state": here("../test/profile-page.svelte.js"),
      "$app/stores": here("../test/profile-stores.js"),
      "$app/navigation": here("./navigation.js"),
      "$app/environment": here("../test/app-environment-stub.js"),
    },
  },
  build: { target: "es2022", outDir: here("../trips-fixture-preview") },
});
