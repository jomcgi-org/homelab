import { defineConfig } from "vitest/config";

export default defineConfig({
  esbuild: { jsx: "automatic" },
  test: {
    environment: "happy-dom",
    include: ["src/**/*.test.{js,ts,tsx}"],
    setupFiles: ["src/test/setup.ts"],
    css: false,
  },
});
