import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";

// A separate module graph compiles actual package imports for svelte/server.
export async function serverRender(name, props = {}) {
  const server = await createServer({
    configFile: false,
    root: resolve("test/data-display"),
    plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
    // Vite 6's SSR condition list is separate from the client list. Opt into
    // the component entry and compile it rather than using Node's core entry.
    ssr: {
      noExternal: ["@homelab/design-system"],
      resolve: { conditions: ["svelte", "node", "module"] },
    },
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null },
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
