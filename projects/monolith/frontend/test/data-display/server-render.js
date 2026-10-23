import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";
import { afterAll } from "vitest";

// Vitest isolates this module per test file. Compile once per file, but render
// afresh for every assertion. Keep loading lazy so the SSR tests still import
// the components with browser globals absent on their first render.
let server;
let entry;

afterAll(async () => {
  if (!entry) return;
  // Failed imports still need cleanup; a failed render does not poison entry.
  await entry.catch(() => {});
  await server?.close();
});

async function loadEntry() {
  server = await createServer({
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
  return server.ssrLoadModule("/server.js");
}

export async function serverRender(name, props = {}) {
  entry ??= loadEntry();
  const renderer = await entry;
  return name
    ? renderer.renderDisplayComponent(name, props)
    : renderer.renderDisplayFixture(props);
}
