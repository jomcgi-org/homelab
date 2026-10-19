import { createServer } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";
import { resolve } from "node:path";
import { alias } from "./vite.config.js";

// A separate server compilation graph keeps Svelte's SSR runtime separate
// from the browser-condition runtime used by the DOM tests.
export async function withServerRender(check) {
  const server = await createServer({
    configFile: false,
    root: resolve("dr-jobs-preview"),
    plugins: [svelte({ configFile: false, compilerOptions: { hmr: false } })],
    resolve: { alias, dedupe: ["svelte"] },
    ssr: { noExternal: ["@homelab/design-system"] },
    optimizeDeps: { noDiscovery: true, exclude: ["svelte"] },
    server: { middlewareMode: true, watch: null, hmr: false, ws: false },
    appType: "custom",
  });
  try {
    const { renderFixture } = await server.ssrLoadModule("/server.js");
    return await check(renderFixture);
  } finally {
    await server.close();
  }
}
