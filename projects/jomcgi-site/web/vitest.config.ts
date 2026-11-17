import { defineConfig } from "vitest/config";
import { blogContent } from "./blog-content.mjs";

export default defineConfig({
  plugins: [blogContent()],
  esbuild: { jsx: "automatic" },
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.tsx"],
    setupFiles: ["src/setup.ts"],
    css: false,
  },
});
