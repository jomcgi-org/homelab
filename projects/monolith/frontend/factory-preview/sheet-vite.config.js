import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
const source = process.env.SHEET_SOURCE_ROOT ?? here("../src");
export default defineConfig({
  root: here("./"),
  base: "./",
  publicDir: false,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "sheet-fixture-boundary",
      generateBundle() {
        for (const moduleId of this.getModuleIds()) {
          const id = moduleId.split("?")[0];
          if (
            /\/lib\/server\/|\+page\.server\.|posts-manifest\.json/.test(id) ||
            (/\/routes\/(private|friends)\//.test(id) &&
              !/\/private\/(\+layout\.svelte|updates\/(\+page\.svelte|updates\.js))$/.test(
                id,
              )) ||
            (/\/lib\/private\//.test(id) &&
              !id.includes("/lib/private/dashboard-theme.css"))
          )
            this.error(`Non-fixture data entered sheet preview: ${id}`);
        }
        const harness = process.env.FACTORY_PREVIEW_SHA ?? "working-tree";
        this.emitFile({
          type: "asset",
          fileName: "sheet-build.json",
          source: JSON.stringify({
            fixture_only: true,
            harness_sha: harness,
            production_sha: process.env.SHEET_SOURCE_REVISION ?? harness,
          }),
        });
      },
    },
  ],
  resolve: {
    conditions: ["browser"],
    alias: [
      {
        find: "@homelab/design-system",
        replacement: here("../../../design-system"),
      },
      { find: "@production", replacement: source },
      {
        find: /^\.\/qwen-replay\.json$/,
        replacement: here("../preview/fixtures/qwen-replay.json"),
      },
      {
        find: "$lib/public/posts/agent-replay.json",
        replacement: here("../preview/fixtures/agent-replay.json"),
      },
      {
        find: "$lib/public/posts/ConformanceReplay.svelte",
        replacement: here("../preview/UnsupportedScenario.svelte"),
      },
      {
        find: /^\$lib\/public\/components$/,
        replacement: here("./sheet-components.js"),
      },
      { find: "$lib", replacement: source + "/lib" },
      { find: "$app/state", replacement: here("./sheet-state.svelte.js") },
      { find: "$app/stores", replacement: here("./sheet-stores.js") },
      { find: "$app/navigation", replacement: here("./sheet-navigation.js") },
      { find: "$app/environment", replacement: here("./environment.js") },
    ],
  },
  build: {
    target: "es2022",
    outDir: process.env.SHEET_OUT_DIR ?? here("../sheet-fixture-preview"),
    emptyOutDir: true,
    rollupOptions: { input: here("./sheets.html") },
  },
});
