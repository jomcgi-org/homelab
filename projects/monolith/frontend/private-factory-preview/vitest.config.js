import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { aliases } from "./vite.config.js";
export default defineConfig({
  plugins: [svelte({ configFile: false })],
  resolve: { conditions: ["browser"], alias: aliases },
  test: {
    environment: "happy-dom",
    include: ["private-factory-preview/*.test.js"],
  },
});
