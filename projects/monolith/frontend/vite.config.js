import { sveltekit } from "@sveltejs/kit/vite";
import { defineConfig } from "vite";
import path from "node:path";

// The Svelte plugin's virtual-style resolver drops Kit's ?inline parameter.
// Preserve it in dev so Vite exports the CSS string Kit needs for SSR.
function inlineSvelteStyles() {
  let root;
  return {
    name: "inline-svelte-styles",
    apply: "serve",
    enforce: "pre",
    configResolved(config) {
      root = config.root;
    },
    resolveId(id) {
      if (id.includes("inline") && id.includes("svelte&type=style")) {
        return id.startsWith("/src/") ? path.join(root, id) : id;
      }
    },
  };
}

export default defineConfig({
  plugins: [inlineSvelteStyles(), sveltekit()],
  resolve: {
    // Svelte 5's package.json `exports` uses `browser` for the client
    // bundle and `default` for the server. If `browser` isn't in the
    // resolved conditions, you get `index-server.js` on the client —
    // onMount becomes a noop and onDestroy crashes accessing
    // ssr_context.r. Pinning the conditions explicitly avoids that.
    conditions: ["browser", "module", "import", "default"],
  },
  build: {
    target: "es2022",
  },
  ssr: {
    // The runtime image ships only the SvelteKit output dir (remapped to
    // /app/build at image time); there is no node_modules. Any package
    // imported by SSR-rendered code must be bundled into the server chunks,
    // not externalized.
    noExternal: [
      "@dagrejs/dagre",
      "d3-quadtree",
      "d3-selection",
      "d3-zoom",
      "marked",
    ],
  },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
