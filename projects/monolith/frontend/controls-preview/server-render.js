import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";

// Match theme-preview's separate server/client compilation graphs.
export async function renderFixture(props = {}) {
  const server = await createServer({
    configFile: false,
    root: resolve("controls-preview"),
    plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
    resolve: { dedupe: ["svelte"] },
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null },
    appType: "custom",
  });
  try {
    const entry = await server.ssrLoadModule("/server.js");
    for (const id of server.moduleGraph.idToModuleMap.keys()) {
      if (/\/projects\/monolith\/frontend\/src\/|\$app\//.test(id))
        throw new Error(
          `Production module entered controls SSR fixture: ${id}`,
        );
    }
    return entry.renderFixture(props);
  } finally {
    await server.close();
  }
}
