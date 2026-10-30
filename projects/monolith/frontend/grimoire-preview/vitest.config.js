import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { alias } from "./vite.config.js";
export default defineConfig({
  plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
  resolve: { conditions: ["browser"], alias },
  test: {
    environment: "happy-dom",
    environmentOptions: {
      happyDOM: {
        settings: {
          handleDisabledFileLoadingAsSuccess: true,
          disableCSSFileLoading: true,
          disableJavaScriptFileLoading: true,
        },
      },
    },
    include: ["grimoire-preview/**/*.test.js"],
  },
});
