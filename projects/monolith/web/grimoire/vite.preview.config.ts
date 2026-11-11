import { defineConfig, mergeConfig } from "vite";
import app from "./vite.config";

// Builds the fixture preview: a static bundle that needs no backend.
export default mergeConfig(
  app,
  defineConfig({
    root: "preview",
    base: "./",
    build: { outDir: "../dist-preview", emptyOutDir: true },
  }),
);
