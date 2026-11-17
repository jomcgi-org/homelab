import { resolve } from "node:path";
import { searchForWorkspaceRoot } from "vite";
import { defineConfig } from "vitest/config";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { blogContent } from "./blog-content.mjs";

export default defineConfig({
  plugins: [blogContent(), svelte()],
  resolve: { conditions: ["browser"] },
  esbuild: { jsx: "automatic" },
  optimizeDeps: { esbuildOptions: { target: "es2022" } },
  // Bazel provides the sibling fixtures without pnpm-workspace.yaml. Allow
  // their raw glob imports even when Vite treats this package as its root.
  server: {
    fs: {
      allow: [
        searchForWorkspaceRoot(process.cwd()),
        resolve(process.cwd(), "../../factory-storyboard/fixtures"),
      ],
    },
  },
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.tsx"],
    setupFiles: ["src/setup.ts"],
    css: false,
  },
});
