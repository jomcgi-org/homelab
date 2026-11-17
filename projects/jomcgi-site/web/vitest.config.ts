import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { blogContent } from "./blog-content.mjs";

export default defineConfig({
  plugins: [blogContent(), svelte()],
  resolve: { conditions: ["browser"] },
  esbuild: { jsx: "automatic" },
  optimizeDeps: { esbuildOptions: { target: "es2022" } },
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.tsx"],
    setupFiles: ["src/setup.ts"],
    css: false,
  },
});
