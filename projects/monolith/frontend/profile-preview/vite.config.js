import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  root: here("./"),
  publicDir: false,
  plugins: [svelte({ configFile: false })],
  resolve: {
    conditions: ["browser"],
    alias: [
      { find: /^maplibre-gl$/, replacement: here("./maplibre-probe.js") },
      ...Object.entries({
        $lib: here("../src/lib"),
        "$app/state": here("../test/profile-page.svelte.js"),
        "$app/stores": here("../test/profile-stores.js"),
        "$app/navigation": here("./navigation.js"),
        "$app/environment": here("./environment.js"),
      }).map(([find, replacement]) => ({ find, replacement })),
    ],
  },
  server: { host: "127.0.0.1" },
  optimizeDeps: {
    exclude: ["maplibre-gl/dist/maplibre-gl.mjs"],
    esbuildOptions: { target: "es2022" },
  },
  build: { target: "es2022", outDir: here("../profile-fixture-dist") },
});
