import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";

// Separate Node and client graphs follow controls-preview's SSR/hydration seam.
export async function renderFixture(props = {}) {
  const server = await createServer({
    configFile: false,
    root: resolve("theme-preview/gallery"),
    plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
    resolve: { dedupe: ["svelte"] },
    ssr: { noExternal: ["@homelab/design-system"] },
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const entry = await server.ssrLoadModule("/server.js");
    for (const id of server.moduleGraph.idToModuleMap.keys()) {
      if (/\/projects\/monolith\/frontend\/src\/|\$app\//.test(id))
        throw new Error(`Production module entered gallery SSR fixture: ${id}`);
    }
    return entry.renderFixture(props);
  } finally {
    await server.close();
  }
}
