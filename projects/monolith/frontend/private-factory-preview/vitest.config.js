import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { aliases } from "./vite.config.js";
export default defineConfig({
  plugins: [svelte({ configFile: false })],
  resolve: { conditions: ["browser"], alias: aliases },
  test: {
    environment: "happy-dom",
    environmentOptions: {
      happyDOM: {
        settings: {
          disableCSSFileLoading: true,
          handleDisabledFileLoadingAsSuccess: true,
        },
      },
    },
    include: ["private-factory-preview/*.test.js"],
  },
});
