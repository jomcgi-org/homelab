import { defineConfig } from "vite";

export default defineConfig({
  root: "factory",
  base: "/",
  esbuild: { jsx: "automatic" },
  build: {
    outDir: "../dist-factory",
    emptyOutDir: true,
    target: "es2022",
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    rollupOptions: {
      onwarn(warning, warn) {
        if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning);
      },
    },
  },
});
