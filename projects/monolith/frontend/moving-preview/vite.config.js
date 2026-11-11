import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
import { load } from "../src/routes/friends/moving/+page.server.js";
import { movingState } from "../test/moving-fixtures.js";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
const moduleId = "virtual:moving-pages";
export default defineConfig({
  root: here("./"),
  base: "./",
  publicDir: false,
  plugins: [
    svelte({ configFile: false }),
    {
      name: "synthetic-moving-loader-fixture",
      resolveId(id) {
        if (id === moduleId) return `\0${moduleId}`;
      },
      async load(id) {
        if (id !== `\0${moduleId}`) return;
        const pages = {};
        const previous = process.env.API_BASE;
        process.env.API_BASE = "https://moving-api.example.test";
        try {
          for (const variant of [
            "populated",
            "empty",
            "forbidden",
            "unavailable",
          ]) {
            pages[variant] = {};
            for (const mode of ["dashboard", "manage"]) {
              const url = new URL(
                `https://friends.example.test/moving${mode === "manage" ? "?mode=manage" : ""}`,
              );
              pages[variant][mode] = await load({
                url,
                request: new Request(url),
                fetch: async (endpoint) => {
                  if (
                    !/^https:\/\/moving-api\.example\.test\/api\/moving\/state\?scope=(mine|all)$/.test(
                      endpoint,
                    )
                  )
                    throw new Error(`Unexpected fixture endpoint: ${endpoint}`);
                  return new Response(
                    JSON.stringify(movingState(variant === "empty")),
                    {
                      status:
                        variant === "forbidden"
                          ? 403
                          : variant === "unavailable"
                            ? 503
                            : 200,
                    },
                  );
                },
              });
            }
          }
        } finally {
          if (previous === undefined) delete process.env.API_BASE;
          else process.env.API_BASE = previous;
        }
        return `export default ${JSON.stringify(pages)};`;
      },
      generateBundle() {
        for (const id of this.getModuleIds()) {
          if (
            /src\/lib\/(server|private)\/|src\/routes\/private\/|\+page\.server\./.test(
              id,
            )
          )
            this.error(`Server data entered preview: ${id}`);
        }
        this.emitFile({
          type: "asset",
          fileName: "build.json",
          source: JSON.stringify({
            commit: process.env.MOVING_PREVIEW_SHA ?? "local-unpublished",
            scope: "synthetic moving fixture, no live acceptance",
          }),
        });
      },
    },
  ],
  resolve: {
    conditions: ["browser"],
    alias: [
      {
        find: /^\$lib\/public\/components$/,
        replacement: here("./components.js"),
      },
      { find: "$lib", replacement: here("../src/lib") },
      { find: "$app/stores", replacement: here("./stores.js") },
      { find: "$app/navigation", replacement: here("./navigation.js") },
    ],
  },
  build: {
    target: "es2022",
    outDir: process.env.MOVING_PREVIEW_OUT ?? here("../moving-fixture-preview"),
    emptyOutDir: true,
  },
});
