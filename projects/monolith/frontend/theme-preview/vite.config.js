import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
import { renderFixture } from "./server-render.js";
const here = (path) => fileURLToPath(new URL(path, import.meta.url));

export default defineConfig({
  root: here("./"),
  base: "./",
  publicDir: false,
  plugins: [
    svelte({ configFile: false, compilerOptions: { hmr: false } }),
    {
      name: "synthetic-theme-ssr",
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
          if (/\/projects\/monolith\/frontend\/src\/|\$app\//.test(id))
            this.error(`Production module entered theme fixture: ${id}`);
        }
        this.emitFile({
          type: "asset",
          fileName: "build.json",
          source: JSON.stringify({
            commit: process.env.FACTORY_PREVIEW_SHA ?? "local-unpublished",
            scope: "synthetic technical-drawing themes only",
          }),
        });
      },
    },
  ],
  resolve: { conditions: ["browser"] },
  build: {
    target: "es2022",
    outDir: here("../theme-fixture-preview"),
    emptyOutDir: true,
    sourcemap: false,
  },
});
