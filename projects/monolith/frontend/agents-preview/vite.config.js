import { createServer, defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { fileURLToPath } from "node:url";
import profile from "../profile-preview/vite.config.js";

const here = (path) => fileURLToPath(new URL(path, import.meta.url));

export default defineConfig({
  root: here("./"),
  publicDir: false,
  resolve: profile.resolve,
  plugins: [
    svelte({ configFile: false, compilerOptions: { hmr: false } }),
    {
      name: "agents-production-ssr",
      transformIndexHtml: {
        order: "pre",
        async handler(html, context) {
          const server = await createServer({
            configFile: false,
            root: here("./"),
            plugins: [svelte({ configFile: false })],
            resolve: { alias: profile.resolve.alias },
            optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
            server: { middlewareMode: true, watch: null, hmr: false },
            appType: "custom",
          });
          try {
            const entry = await server.ssrLoadModule("/server.js");
            const rendered = entry.renderFixture(
              context.filename.endsWith("/banner.html"),
            );
            return html
              .replace("<!--agents-ssr-->", rendered.body)
              .replace("</head>", `${rendered.head}</head>`);
          } finally {
            await server.close();
          }
        },
      },
      generateBundle() {
        this.emitFile({
          type: "asset",
          fileName: "build.json",
          source: JSON.stringify({
            commit: process.env.AGENTS_PREVIEW_SHA ?? "local-unpublished",
          }),
        });
      },
    },
  ],
  build: {
    target: "es2022",
    outDir: here("../agents-fixture-dist"),
    rollupOptions: {
      input: { index: here("./index.html"), banner: here("./banner.html") },
    },
  },
});
