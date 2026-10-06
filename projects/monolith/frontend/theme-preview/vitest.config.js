import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
  resolve: { conditions: ["browser"] },
  test: { environment: "node", include: ["theme-preview/**/*.test.js"] },
});
