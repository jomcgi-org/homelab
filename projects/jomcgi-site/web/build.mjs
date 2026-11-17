// Builds the site, then writes each page's server-rendered HTML into its built
// entry. A page reads fully, with its real preview tags, before any script
// runs; the browser then hydrates the same tree (HOME-178 performance contract).
// Usage: node build.mjs (from this directory)
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { build, createServer } from "vite";

await build();

// Load prerender.tsx through Vite, as the tests do, without a second bundle.
const server = await createServer({
  appType: "custom",
  logLevel: "warn",
  cacheDir: join(tmpdir(), "jomcgi-site-vite"),
  optimizeDeps: {
    noDiscovery: true,
    include: [],
    esbuildOptions: { target: "es2022" },
  },
  server: { middlewareMode: true, hmr: false, watch: null },
});
try {
  const { MARK, render } = await server.ssrLoadModule("/src/prerender.tsx");
  const { marketplace } = await server.ssrLoadModule("/src/scotland-data.ts");
  await mkdir("dist/scotland", { recursive: true });
  await writeFile(
    "dist/scotland/marketplace.json",
    `${JSON.stringify(marketplace, null, 2)}\n`,
  );
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
