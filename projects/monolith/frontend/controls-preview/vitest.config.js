import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
  resolve: { conditions: ["browser"], dedupe: ["svelte"] },
  test: { environment: "node", include: ["controls-preview/*.test.js"] },
});
