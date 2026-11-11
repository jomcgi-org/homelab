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
      name: "stars-fixture-provenance",
      generateBundle() {
        this.emitFile({
          type: "asset",
          fileName: "build.json",
          source: JSON.stringify({
            commit: process.env.STARS_PREVIEW_SHA ?? "unstamped",
            scope: "synthetic stars data, production map and layouts",
          }),
        });
        this.emitFile({
          type: "asset",
          fileName: "assets/SchibstedGrotesk-LICENSE.txt",
          source: readFileSync(here("../preview/fonts/OFL.txt"), "utf8"),
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
      {
        find: "$app/state",
        replacement: here("../test/profile-page.svelte.js"),
      },
      { find: "$app/stores", replacement: here("../test/profile-stores.js") },
      { find: "$app/navigation", replacement: here("./navigation.js") },
      { find: "$app/environment", replacement: here("./environment.js") },
    ],
  },
  server: { host: "127.0.0.1" },
  build: { target: "es2022", outDir: here("../stars-fixture-preview") },
});
