// Builds the site, then writes each page's server-rendered HTML into its built
// entry. A page reads fully, with its real preview tags, before any script
// runs; the browser then hydrates the same tree (HOME-178 performance contract).
// Usage: node build.mjs (from this directory)
import { readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build, createServer } from "vite";

await build();

// Load prerender.tsx through Vite, as the tests do, without a second bundle.
const server = await createServer({
  appType: "custom",
  logLevel: "warn",
  cacheDir: join(tmpdir(), "jomcgi-site-vite"),
  optimizeDeps: { noDiscovery: true, include: [] },
  server: { middlewareMode: true, hmr: false, watch: null },
});
try {
  const { MARK, render } = await server.ssrLoadModule("/src/prerender.tsx");
  const { PAGES } = await server.ssrLoadModule("/src/pages.tsx");
  for (const entry of Object.keys(PAGES)) {
    const file = join("dist", entry);
    const parts = (await readFile(file, "utf8")).split(MARK);
    if (parts.length !== 2) {
      throw new Error(`${entry} must hold exactly one ${MARK}`);
    }
    await writeFile(file, parts.join(render(entry)));
  }
} finally {
  await server.close();
}

// The deploy-time snapshot renders the variable trip/day URLs after reading R2.
// Keep its Node-only bundle separate from the content-named browser chunks.
await build({
  configFile: false,
  publicDir: false,
  esbuild: { jsx: "automatic" },
  ssr: { noExternal: true },
  build: {
    ssr: "src/trips-render.tsx",
    outDir: "dist/.trips-renderer",
    target: "node22",
    rollupOptions: {
      external: ["maplibre-gl", "maplibre-gl/dist/maplibre-gl.css"],
      output: {
        entryFileNames: "render.mjs",
        chunkFileNames: "[name]-[hash].mjs",
      },
      onwarn(warning, warn) {
        if (warning.code !== "MODULE_LEVEL_DIRECTIVE") warn(warning);
      },
    },
  },
});
