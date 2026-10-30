import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export const alias = {
  $lib: here("../src/lib"),
  "$app/state": here("./page.svelte.js"),
  "$app/stores": here("./stores.js"),
  "$app/navigation": here("./navigation.js"),
  "$app/environment": here("../test/app-environment-stub.js"),
};
export default defineConfig({
  root: here("./"),
  publicDir: false,
  plugins: [svelte({ configFile: false })],
  resolve: { conditions: ["browser"], alias },
  server: { host: "127.0.0.1" },
  build: { target: "es2022", outDir: here("../grimoire-fixture-preview") },
});
