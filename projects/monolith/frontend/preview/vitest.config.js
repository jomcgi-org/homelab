import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
export default defineConfig({
  resolve: {
    alias: [
      {
        find: "$lib/public/posts/posts-manifest.json",
        replacement: here("./fixtures/posts.json"),
      },
      { find: "$lib", replacement: here("../src/lib") },
      {
        find: "$app/environment",
        replacement: here("../test/app-environment-stub.js"),
      },
      { find: "$env/dynamic/private", replacement: here("./empty-env.js") },
    ],
  },
  test: { environment: "node", include: ["preview/contract.test.js"] },
});
