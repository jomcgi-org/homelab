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
    alias: [
      { find: /^maplibre-gl$/, replacement: here("./maplibre.js") },
      { find: "$lib", replacement: here("../src/lib") },
      {
        find: "$app/state",
        replacement: here("../test/profile-page.svelte.js"),
      },
      { find: "$app/stores", replacement: here("../test/profile-stores.js") },
      { find: "$app/navigation", replacement: here("./navigation.js") },
      {
        find: "$app/environment",
        replacement: here("../test/app-environment-stub.js"),
      },
    ],
  },
  build: { target: "es2022", outDir: here("../trips-fixture-preview") },
});
