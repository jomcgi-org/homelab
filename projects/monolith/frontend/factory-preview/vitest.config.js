import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  resolve: { alias: { "$app/environment": here("./environment.js") } },
  test: {
    environment: "node",
    include: [
      "factory-preview/contract.test.js",
      "src/lib/public/factory/{model,activity-view,charts,search-index}.test.js",
    ],
  },
});
