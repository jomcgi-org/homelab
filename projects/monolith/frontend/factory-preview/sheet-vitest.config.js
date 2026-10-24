import { defineConfig, mergeConfig } from "vitest/config";
import preview from "./sheet-vite.config.js";

export default mergeConfig(
  preview,
  defineConfig({
    test: {
      environment: "happy-dom",
      include: ["sheet.dom.test.js", "sheet.ssr.test.js"],
      globalSetup: ["./sheet-setup.js"],
    },
  }),
);
