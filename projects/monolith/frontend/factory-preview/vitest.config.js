import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
import { svelte } from "@sveltejs/vite-plugin-svelte";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
  resolve: {
    conditions: ["browser"],
    alias: [
      {
        find: /^\$lib\/public\/components$/,
        replacement: here("./components.js"),
      },
      { find: "$lib", replacement: here("../src/lib") },
      { find: "$app/environment", replacement: here("./environment.js") },
      { find: "$app/stores", replacement: here("./execution-stores.js") },
      { find: "$app/state", replacement: here("../test/app-state-stub.js") },
      {
        find: "$app/navigation",
        replacement: here("../test/app-navigation-stub.js"),
      },
    ],
  },
  test: {
    environment: "node",
    environmentOptions: {
      happyDOM: {
        settings: {
          navigation: {
            disableMainFrameNavigation: true,
            disableChildFrameNavigation: true,
            disableChildPageNavigation: true,
          },
        },
      },
    },
    include: [
      "factory-preview/contract.test.js",
      "factory-preview/execution.dom.test.js",
      "src/routes/public/slop/factory/page.test.js",
      "src/lib/public/factory/{model,activity-view,charts,search-index}.test.js",
      "src/routes/private/factory/execution/*.test.js",
      "src/routes/private/factory/execution/drain/*.test.js",
      "src/lib/public/styles/technical-drawing.test.js",
    ],
  },
});
