import { defineConfig } from "vite";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { contentSecurityPolicy } from "./csp";

const fixtures = fileURLToPath(new URL("../fixtures/", import.meta.url));

export default defineConfig({
  base: "/",
  plugins: [{
    name: "storyboard-fixtures",
    transformIndexHtml(_, context) {
      return context.server ? [] : [{
        tag: "meta",
        attrs: { "http-equiv": "Content-Security-Policy", content: contentSecurityPolicy },
        injectTo: "head-prepend" as const,
      }];
    },
    // Development only. The production Worker will serve /data/* from R2.
    configureServer(server) {
      server.middlewares.use("/data", async (request, response) => {
        const path = request.url?.split("?")[0] ?? "";
        if (!/^\/(manifest\.json|objects\/[a-f0-9]{64}\.json)$/.test(path)) {
          response.statusCode = 404;
          response.end("Not available");
          return;
        }
        try {
          const content = await readFile(`${fixtures}${path.slice(1)}`);
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.setHeader("Cache-Control", "no-store");
          response.end(content);
        } catch {
          response.statusCode = 404;
          response.setHeader("Content-Type", "application/json; charset=utf-8");
          response.end(JSON.stringify({ error: "Not available" }));
        }
      });
    },
  }],
  esbuild: { jsx: "automatic" },
  build: {
    outDir: "dist",
    target: "es2022",
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    rollupOptions: {
      onwarn(warning, warn) {
        if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning);
      },
    },
  },
});
