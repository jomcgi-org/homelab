import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";

// A separate Node module graph compiles the component for svelte/server.
// The test/client graph compiles the same source for hydration.
export async function renderFixture() {
  const server = await createServer({
    configFile: false,
    root: resolve("theme-preview"),
    plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
    ssr: {
      noExternal: ["@homelab/design-system"],
    },
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    // Vite still opens a WebSocket listener when only HMR is disabled.
    server: { middlewareMode: true, watch: null, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const entry = await server.ssrLoadModule("/server.js");
    for (const id of server.moduleGraph.idToModuleMap.keys()) {
      if (/\/projects\/monolith\/frontend\/src\/|\$app\//.test(id))
        throw new Error(`Production module entered theme SSR fixture: ${id}`);
    }
    return entry.renderFixture();
  } finally {
    await server.close();
  }
}
