import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
import { renderFixture } from "./server-render.js";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));
const forbidden = (id) =>
  /\/projects\/monolith\/frontend\/src\/|\$app\//.test(id);

export default defineConfig({
  root: here("./"),
  base: "./",
  publicDir: false,
  plugins: [
    svelte({ configFile: false, compilerOptions: { hmr: false } }),
    {
      name: "synthetic-controls-ssr",
      resolveId(id) {
        if (forbidden(id))
          this.error(`Production module entered controls fixture: ${id}`);
      },
      transformIndexHtml: {
        order: "pre",
        async handler(html) {
          const rendered = await renderFixture();
          return html
            .replace("<!--fixture-ssr-->", rendered.body)
            .replace("</head>", `${rendered.head}</head>`);
        },
      },
      generateBundle() {
        for (const id of this.getModuleIds()) {
          if (forbidden(id))
            this.error(`Production module entered controls fixture: ${id}`);
        }
        this.emitFile({
          type: "asset",
          fileName: "build.json",
          source: JSON.stringify({
            commit: process.env.FACTORY_PREVIEW_SHA ?? "local-unpublished",
            scope: "synthetic shared controls only",
          }),
        });
      },
    },
  ],
  resolve: { conditions: ["browser"], dedupe: ["svelte"] },
  build: {
    target: "es2022",
    outDir: here("../controls-fixture-preview"),
    emptyOutDir: true,
    sourcemap: false,
  },
});
