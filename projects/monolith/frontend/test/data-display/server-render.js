import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";

// A separate module graph compiles actual package imports for svelte/server.
export async function serverRender(name, props = {}) {
  const server = await createServer({
    configFile: false,
    root: resolve("test/data-display"),
    plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
    // The package exposes one component entry under every condition, so
    // default SSR resolution compiles the same components as the client.
    // No hand-set conditions: this harness must resolve like the real app.
    ssr: {
      noExternal: ["@homelab/design-system"],
    },
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null, hmr: false },
    appType: "custom",
  });
  try {
    const entry = await server.ssrLoadModule("/server.js");
    return name
      ? entry.renderDisplayComponent(name, props)
      : entry.renderDisplayFixture(props);
  } finally {
    await server.close();
  }
}
