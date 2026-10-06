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
      resolve: { conditions: ["svelte", "node", "module"] },
    },
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null, hmr: false },
    appType: "custom",
  });
  try {
    const entry = await server.ssrLoadModule("/server.js");
    return entry.renderFixture();
  } finally {
    await server.close();
  }
}
